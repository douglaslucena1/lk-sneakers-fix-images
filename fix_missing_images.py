"""
fix_missing_images.py

Corrige em massa posts do blog LK Sneakers que perderam featured image e carrossel
de imagens (galeria .lk-gallery) por falha da automação de conteúdo a partir de 20/mar/2026.

Pipeline:
  1. Lista posts mais recentes com `featured_media == 0` via REST API
  2. Extrai backlink "Fonte: ..." (sneakernews.com) do content
  3. Scrapa imagens da página fonte
  4. Baixa, valida e faz upload pro WP Media Library
  5. Atualiza o post: define featured_media + prepende galeria <div class="lk-gallery">
  6. Gera relatório CSV em reports/

Uso:
  python fix_missing_images.py --dry-run --single-post 13538   # debug 1 post sem aplicar
  python fix_missing_images.py --dry-run                       # dry-run em N posts
  python fix_missing_images.py                                  # rodada real
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from PIL import Image
except ImportError:
    print("Pillow não instalado. Rode: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)


# =========================================================================
# Constantes (configuráveis)
# =========================================================================
MAX_POSTS = 15
MAX_IMAGES_PER_POST = 8
MIN_IMAGE_BYTES = 10 * 1024
MIN_IMAGE_WIDTH = 600
SLEEP_BETWEEN_POSTS = 2.5
HTTP_TIMEOUT = 20
UPLOAD_TIMEOUT = 60
ALLOWED_SOURCE_DOMAINS = {"sneakernews.com", "www.sneakernews.com"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ROOT = Path(__file__).parent
TMP_DIR = ROOT / "tmp"
LOGS_DIR = ROOT / "logs"
REPORTS_DIR = ROOT / "reports"


# =========================================================================
# Logging com sanitização da senha
# =========================================================================
class SecretFilter(logging.Filter):
    """Remove a senha de aplicação de qualquer log message."""
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self._secrets:
            if s and s in msg:
                record.msg = msg.replace(s, "***REDACTED***")
                record.args = ()
        return True


def setup_logging(app_password: str) -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOGS_DIR / f"run_{timestamp}.log"

    logger = logging.getLogger("fix_images")
    logger.setLevel(logging.DEBUG)
    # Limpa handlers de runs anteriores se reusarmos
    logger.handlers.clear()

    secret_filter = SecretFilter([app_password, app_password.replace(" ", "")])

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(secret_filter)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    ch.addFilter(secret_filter)
    logger.addHandler(ch)

    logger.debug("Log file: %s", log_file)
    return logger


# =========================================================================
# Sessão HTTP com retry
# =========================================================================
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=2,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),  # só GET retry, nunca POST
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# =========================================================================
# 1. Descobrir posts a corrigir
# =========================================================================
def fetch_posts_to_fix(
    session: requests.Session, wp_url: str, log: logging.Logger
) -> list[dict]:
    url = (
        f"{wp_url}/wp-json/wp/v2/posts"
        "?per_page=50&page=1&orderby=date&order=desc"
        "&_fields=id,date,slug,link,title,featured_media,content"
    )
    log.debug("GET %s", url)
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    posts = r.json()
    affected = [p for p in posts if p.get("featured_media") == 0]
    log.info(
        "Posts retornados: %d | sem featured_media: %d", len(posts), len(affected)
    )
    return affected[:MAX_POSTS]


def fetch_single_post(
    session: requests.Session, wp_url: str, post_id: int, log: logging.Logger
) -> Optional[dict]:
    url = (
        f"{wp_url}/wp-json/wp/v2/posts/{post_id}"
        "?_fields=id,date,slug,link,title,featured_media,content"
    )
    log.debug("GET %s", url)
    r = session.get(url, timeout=HTTP_TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


# =========================================================================
# 2. Extrair backlink
# =========================================================================
def extract_source_url(content_html: str) -> Optional[str]:
    soup = BeautifulSoup(content_html, "lxml")
    # Última <a> dentro de <p> que comece com "Fonte"
    for p in reversed(soup.find_all("p")):
        text = p.get_text(" ", strip=True)
        if text.lower().startswith("fonte"):
            a = p.find("a", href=True)
            if a:
                return a["href"]
    # Fallback: qualquer link sneakernews.com
    for a in soup.find_all("a", href=True):
        if "sneakernews.com" in a["href"]:
            return a["href"]
    return None


def is_allowed_source(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return host in ALLOWED_SOURCE_DOMAINS
    except Exception:
        return False


# =========================================================================
# 3. Scrapar imagens da fonte
# =========================================================================
_BAD_SUBSTRINGS = (
    "logo", "avatar", "gravatar", "icon",
    "advert", "sponsor", "/themes/", "/plugins/",
    "1x1", "spinner", "loader",
)


def _largest_from_srcset(srcset: str) -> Optional[str]:
    """Retorna a URL com maior width do srcset (formato 'url1 800w, url2 1200w')."""
    items: list[tuple[int, str]] = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0]
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                pass
        items.append((width, url))
    if not items:
        return None
    items.sort(key=lambda t: t[0], reverse=True)
    return items[0][1]


def scrape_images(
    source_url: str, session: requests.Session, log: logging.Logger
) -> list[str]:
    log.debug("scrape_images: %s", source_url)
    # Headers ricos pra mitigar bot detection (Cloudflare, etc)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8,pt;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    r = session.get(source_url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    container = soup.select_one("div.entry-content, article .entry-content, article")
    if not container:
        log.warning("scrape_images: container .entry-content não encontrado")
        return []

    urls: list[str] = []
    seen: set[str] = set()

    for img in container.find_all("img"):
        candidate: Optional[str] = None

        # 1. Preferência: <a> parent apontando pra full-res
        parent_a = img.find_parent("a", href=True)
        if parent_a and re.search(r"\.(jpe?g|png|webp)(\?|$)", parent_a["href"], re.I):
            candidate = parent_a["href"]
        # 2. srcset (maior largura)
        elif img.get("srcset"):
            candidate = _largest_from_srcset(img["srcset"])
        elif img.get("data-srcset"):
            candidate = _largest_from_srcset(img["data-srcset"])
        # 3. fallback src / data-src
        if not candidate:
            candidate = img.get("src") or img.get("data-src") or img.get("data-lazy-src")

        if not candidate:
            continue

        # Normaliza URLs relativas
        if candidate.startswith("//"):
            candidate = "https:" + candidate

        low = candidate.lower()
        if any(bad in low for bad in _BAD_SUBSTRINGS):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
        if len(urls) >= MAX_IMAGES_PER_POST:
            break

    log.info("scrape_images: %d URLs candidatas", len(urls))
    return urls


# =========================================================================
# 4. Download e validação
# =========================================================================
def download_image(
    url: str, session: requests.Session, dest_dir: Path, log: logging.Logger
) -> Optional[Path]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://sneakernews.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        r = session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.warning("download falhou: %s — %s", url, e)
        return None

    ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not ctype.startswith("image/"):
        log.debug("download: content-type não-imagem (%s) %s", ctype, url)
        return None

    body = r.content
    if len(body) < MIN_IMAGE_BYTES:
        log.debug("download: muito pequena (%d bytes) %s", len(body), url)
        return None

    # Valida com Pillow + dimensão
    try:
        Image.open(BytesIO(body)).verify()
        im = Image.open(BytesIO(body))
        if im.width < MIN_IMAGE_WIDTH:
            log.debug("download: width %d < min %d, %s", im.width, MIN_IMAGE_WIDTH, url)
            return None
    except Exception as e:
        log.debug("download: Pillow rejeitou %s — %s", url, e)
        return None

    ext = mimetypes.guess_extension(ctype) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    fname = hashlib.md5(url.encode("utf-8")).hexdigest()[:12] + ext
    fpath = dest_dir / fname
    fpath.write_bytes(body)
    log.debug("download ok: %s -> %s (%d bytes, %dx%d)",
              url, fname, len(body), im.width, im.height)
    return fpath


# =========================================================================
# 5. Upload pro WP Media Library
# =========================================================================
def upload_to_wp(
    fpath: Path,
    post_title: str,
    slug: str,
    index: int,
    session: requests.Session,
    wp_url: str,
    auth: tuple[str, str],
    log: logging.Logger,
) -> Optional[dict]:
    ctype = mimetypes.guess_type(fpath.name)[0] or "image/jpeg"
    headers = {
        "Content-Disposition": f'attachment; filename="{fpath.name}"',
        "User-Agent": USER_AGENT,
    }
    with fpath.open("rb") as fh:
        files = {"file": (fpath.name, fh, ctype)}
        data = {
            "alt_text": post_title,
            "caption": post_title,
            "title": f"{slug}-{index}",
        }
        try:
            r = session.post(
                f"{wp_url}/wp-json/wp/v2/media",
                headers=headers,
                files=files,
                data=data,
                auth=auth,
                timeout=UPLOAD_TIMEOUT,
            )
        except Exception as e:
            log.error("upload exception: %s", e)
            return None

    if r.status_code >= 300:
        log.error("upload falhou %d: %s", r.status_code, r.text[:300])
        return None
    media = r.json()
    log.debug("upload ok: media id=%s url=%s", media.get("id"), media.get("source_url"))
    return media


# =========================================================================
# 6. Construir HTML da galeria + atualizar post
# =========================================================================
GALLERY_SCRIPT = """<p><!-- JS inline para trocar a imagem ao clicar nas miniaturas --><br />
<script>
document.addEventListener('DOMContentLoaded', function(){
  var root = document.querySelector('.lk-gallery');
  if(!root) return;
  var mainImg = root.querySelector('#lk-main-img');
  var thumbs  = root.querySelectorAll('.lk-thumb');
  function select(btn){
    if(!btn || !btn.dataset.src) return;
    thumbs.forEach(function(b){ b.removeAttribute('aria-current'); });
    btn.setAttribute('aria-current','true');
    var img = new Image();
    img.onload = function(){
      mainImg.src = btn.dataset.src;
      mainImg.alt = btn.dataset.alt || '';
    };
    img.src = btn.dataset.src;
  }
  root.addEventListener('click', function(e){
    var btn = e.target.closest('.lk-thumb');
    if(btn) select(btn);
  });
  root.addEventListener('keydown', function(e){
    if(e.key === 'Enter' || e.key === ' '){
      var btn = e.target.closest('.lk-thumb');
      if(btn){ e.preventDefault(); select(btn); }
    }
  });
});
</script></p>
"""


def build_gallery_html(image_urls: list[str], alt_prefix: str) -> str:
    if not image_urls:
        return ""
    main_url = image_urls[0]
    main_alt = f"{alt_prefix}-1"

    thumbs_parts = []
    for i, url in enumerate(image_urls, 1):
        alt = f"{alt_prefix}-{i}"
        aria = ' aria-current="true"' if i == 1 else ""
        thumbs_parts.append(
            f'<button class="lk-thumb" type="button" data-src="{url}" data-alt="{alt}"{aria}><br />\n'
            f'      <img decoding="async" src="{url}" alt="{alt}"><br />\n'
            f"    </button>"
        )
    thumbs_html = "".join(thumbs_parts)

    return (
        '<div class="lk-gallery" role="group" aria-label="Galeria de imagens">\n'
        '<figure class="lk-main">\n'
        f'    <img id="lk-main-img" src="{main_url}" alt="{main_alt}" loading="eager" decoding="async"><br />\n'
        '  </figure>\n'
        '<div class="lk-thumbs" role="list" aria-label="Miniaturas">\n'
        f'    {thumbs_html}</div>\n'
        '</div>\n'
        + GALLERY_SCRIPT
    )


def update_post(
    post_id: int,
    featured_media_id: int,
    new_content: str,
    session: requests.Session,
    wp_url: str,
    auth: tuple[str, str],
    log: logging.Logger,
) -> bool:
    payload = {
        "featured_media": featured_media_id,
        "content": new_content,
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        r = session.post(
            f"{wp_url}/wp-json/wp/v2/posts/{post_id}",
            headers=headers,
            json=payload,
            auth=auth,
            timeout=UPLOAD_TIMEOUT,
        )
    except Exception as e:
        log.error("update_post exception: %s", e)
        return False

    if r.status_code >= 300:
        log.error("update_post falhou %d: %s", r.status_code, r.text[:300])
        return False
    log.debug("update_post ok: id=%d featured=%d", post_id, featured_media_id)
    return True


# =========================================================================
# Pipeline por post
# =========================================================================
def process_post(
    post: dict,
    session: requests.Session,
    wp_url: str,
    auth: tuple[str, str],
    dry_run: bool,
    log: logging.Logger,
    pre_scraped: Optional[list[dict]] = None,
) -> dict:
    """Retorna dict com campos pro CSV."""
    started = time.time()
    result = {
        "post_id": post["id"],
        "slug": post["slug"],
        "link": post.get("link", ""),
        "status": "error",
        "reason": "",
        "source_url": "",
        "images_found": 0,
        "images_uploaded": 0,
        "featured_media_id": 0,
        "time_taken_s": 0,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        title_text = post.get("title", {}).get("rendered", "").strip() or post["slug"]
        original_html = post.get("content", {}).get("rendered", "") or ""

        # Step 2: extract backlink
        source = extract_source_url(original_html)
        if not source:
            result["status"] = "skip"
            result["reason"] = "no_backlink"
            log.warning("[%s] sem backlink — pulando", post["slug"])
            return result
        result["source_url"] = source

        if not is_allowed_source(source):
            result["status"] = "skip"
            result["reason"] = "source_out_of_scope"
            log.info("[%s] fonte fora do escopo: %s", post["slug"], source)
            return result

        # Step 3: scrape (ou usa pre-scrappeado)
        if pre_scraped is not None:
            log.info("[%s] usando %d entradas pre-scrappeadas",
                     post["slug"], len(pre_scraped))
            result["images_found"] = len(pre_scraped)
        else:
            urls = scrape_images(source, session, log)
            pre_scraped = [{"url": u, "local_path": None} for u in urls]
            result["images_found"] = len(urls)

        if not pre_scraped:
            result["status"] = "skip"
            result["reason"] = "no_images_scraped"
            return result

        # Step 4: resolve paths locais (usa local_path se existir, senao download remoto)
        local_paths: list[Path] = []
        for entry in pre_scraped:
            local_path_str = entry.get("local_path")
            if local_path_str:
                lp = Path(local_path_str)
                if not lp.is_absolute():
                    lp = ROOT / lp
                if lp.exists():
                    local_paths.append(lp)
                else:
                    log.warning("[%s] local_path nao existe: %s", post["slug"], lp)
            else:
                # Fallback: download remoto (pode falhar com Cloudflare)
                p2 = download_image(entry["url"], session, TMP_DIR, log)
                if p2:
                    local_paths.append(p2)

        if not local_paths:
            result["status"] = "skip"
            result["reason"] = "no_valid_images"
            return result

        # Step 5: upload (pulado em dry-run pra nao criar midias fantasmas)
        if dry_run:
            log.info("[%s] DRY RUN - pulando upload de %d imagens (validou paths)",
                     post["slug"], len(local_paths))
            result["images_uploaded"] = len(local_paths)
            result["status"] = "dry_run"
            result["reason"] = "skipped_upload_and_update_dry_run"
            return result

        media_objs: list[dict] = []
        for i, fp in enumerate(local_paths, 1):
            m = upload_to_wp(fp, title_text, post["slug"], i, session, wp_url, auth, log)
            if m:
                media_objs.append(m)
        result["images_uploaded"] = len(media_objs)
        if not media_objs:
            result["status"] = "error"
            result["reason"] = "all_uploads_failed"
            return result

        # Step 6: idempotência — re-checa featured_media
        re_check = fetch_single_post(session, wp_url, post["id"], log)
        if re_check and re_check.get("featured_media", 0) != 0:
            result["status"] = "skip"
            result["reason"] = "already_fixed_concurrent"
            log.warning("[%s] post já foi corrigido em paralelo, skip update", post["slug"])
            return result

        featured_id = media_objs[0]["id"]
        result["featured_media_id"] = featured_id

        gallery_html = build_gallery_html(
            [m["source_url"] for m in media_objs], post["slug"]
        )
        new_content = gallery_html + original_html

        if dry_run:
            result["status"] = "dry_run"
            result["reason"] = "skipped_update_dry_run"
            log.info("[%s] DRY RUN — não atualiza post (uploadou %d imagens)",
                     post["slug"], len(media_objs))
            return result

        # Step 6b: update post
        ok = update_post(
            post["id"], featured_id, new_content, session, wp_url, auth, log
        )
        if ok:
            result["status"] = "success"
            result["reason"] = ""
            log.info("[%s] OK — featured=%d, %d imagens",
                     post["slug"], featured_id, len(media_objs))
        else:
            result["status"] = "error"
            result["reason"] = "update_post_failed"
        return result

    except Exception as e:
        result["status"] = "error"
        result["reason"] = f"exception: {str(e)[:180]}"
        log.exception("[%s] exception", post.get("slug", "?"))
        return result
    finally:
        result["time_taken_s"] = round(time.time() - started, 2)


# =========================================================================
# Main
# =========================================================================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fix missing images in LK Sneakers blog posts")
    p.add_argument("--dry-run", action="store_true", help="não atualiza posts, só simula")
    p.add_argument("--single-post", type=int, default=None, help="processa só esse post ID")
    p.add_argument(
        "--scraped-json",
        type=str,
        default=None,
        help="caminho pra JSON com {post_id: [image_urls]} pré-scrappeado (bypassa Cloudflare)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(ROOT / ".env")

    wp_url = os.getenv("WP_URL", "").rstrip("/")
    wp_user = os.getenv("WP_USER", "")
    wp_pw = os.getenv("WP_APP_PASSWORD", "")

    if not wp_url or not wp_user or not wp_pw:
        print("Faltam WP_URL, WP_USER ou WP_APP_PASSWORD no .env", file=sys.stderr)
        return 2

    log = setup_logging(wp_pw)
    log.info("=== fix_missing_images.py - start (dry_run=%s, single=%s, scraped_json=%s) ===",
             args.dry_run, args.single_post, args.scraped_json)

    auth = (wp_user, wp_pw)
    session = make_session()

    # Carrega JSON pre-scrappeado (formato: { post_id: [{url, local_path, ...}] })
    scraped_data: dict[int, list[dict]] = {}
    if args.scraped_json:
        json_path = Path(args.scraped_json)
        if not json_path.is_absolute():
            json_path = ROOT / json_path
        if not json_path.exists():
            log.error("scraped-json nao encontrado: %s", json_path)
            return 2
        with json_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        for k, v in raw.items():
            # Aceita formato antigo (list[str]) ou novo (list[dict])
            if v and isinstance(v[0], str):
                scraped_data[int(k)] = [{"url": u, "local_path": None} for u in v]
            else:
                scraped_data[int(k)] = v
        log.info("Carregados %d posts pre-scrappeados de %s", len(scraped_data), json_path)

    # Descobrir posts
    if args.single_post:
        post = fetch_single_post(session, wp_url, args.single_post, log)
        if not post:
            log.error("post %d nao encontrado", args.single_post)
            return 1
        if post.get("featured_media", 0) != 0:
            log.warning("post %d ja tem featured_media=%d - nao precisa corrigir",
                        post["id"], post["featured_media"])
            return 0
        posts = [post]
    elif scraped_data:
        # Quando ha scraped_json, processa todos os posts com >=1 imagem
        posts = []
        for pid, entries in scraped_data.items():
            if not entries:
                log.info("post %d sem imagens no scraped-json, pulando", pid)
                continue
            p = fetch_single_post(session, wp_url, pid, log)
            if p is None:
                continue
            if p.get("featured_media", 0) != 0:
                log.info("post %d ja tem featured_media=%d, pulando",
                         pid, p["featured_media"])
            else:
                posts.append(p)
        log.info("Processando %d post(s) do scraped-json", len(posts))
    else:
        posts = fetch_posts_to_fix(session, wp_url, log)

    if not posts:
        log.info("Nenhum post pra corrigir.")
        return 0

    log.info("Processando %d post(s)", len(posts))

    # Setup CSV
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = REPORTS_DIR / f"run_{timestamp}.csv"
    fieldnames = [
        "post_id", "slug", "link", "status", "reason",
        "source_url", "images_found", "images_uploaded",
        "featured_media_id", "time_taken_s", "timestamp",
    ]

    successes = 0
    skips = 0
    errors = 0

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for i, post in enumerate(posts, 1):
            log.info("--- [%d/%d] post %d (%s) ---", i, len(posts), post["id"], post["slug"])
            pre_scraped = scraped_data.get(post["id"]) if scraped_data else None
            if scraped_data and not pre_scraped:
                log.warning("[%s] sem entrada (ou vazia) no scraped-json, pulando", post["slug"])
                row = {
                    "post_id": post["id"], "slug": post["slug"], "link": post.get("link", ""),
                    "status": "skip",
                    "reason": "empty_in_scraped_json" if pre_scraped == [] else "missing_in_scraped_json",
                    "source_url": "", "images_found": 0, "images_uploaded": 0,
                    "featured_media_id": 0, "time_taken_s": 0,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
            else:
                row = process_post(post, session, wp_url, auth, args.dry_run, log, pre_scraped)
            writer.writerow(row)
            f.flush()

            if row["status"] == "success":
                successes += 1
            elif row["status"] == "skip" or row["status"] == "dry_run":
                skips += 1
            else:
                errors += 1

            if i < len(posts):
                time.sleep(SLEEP_BETWEEN_POSTS)

    log.info("=== finalizou ===")
    log.info("Sucesso: %d | Skip/dry_run: %d | Erros: %d", successes, skips, errors)
    log.info("Relatório: %s", csv_path)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
