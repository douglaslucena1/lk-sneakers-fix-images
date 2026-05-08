"""
scrape.py - Scrape posts afetados do blog usando Scrapling (StealthyFetcher).

Pipeline por post:
  1. Lista posts afetados (featured_media=0) via REST API do blog
  2. Pra cada um: extrai backlink "Fonte: ..." (sneakernews.com)
  3. Abre a fonte com Scrapling StealthyFetcher (passa Cloudflare ~34s)
  4. Dentro do page_action: scrape URLs (featured + galeria) e baixa cada imagem via fetch JS
  5. Decodifica base64 e salva em tmp/{post_id}/
  6. Gera scraped.json com {post_id: [{url, local_path, size, content_type}]}

Uso:
  python scrape.py --single-post 13538       # 1 post (debug)
  python scrape.py --max-posts 15            # piloto
  python scrape.py --max-posts 100 --skip 1  # batch maior, pulando o post 13538 ja resolvido

Tempo: ~35s por post (Cloudflare challenge resolution).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from scrapling.fetchers import StealthyFetcher

ROOT = Path(__file__).parent
TMP_DIR = ROOT / "tmp"

ALLOWED_DOMAINS = {"sneakernews.com", "www.sneakernews.com"}
MAX_IMAGES_PER_POST = 8

log = logging.getLogger("scrape")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Reduz ruido do scrapling
    logging.getLogger("scrapling").setLevel(logging.WARNING)


def fetch_posts_to_fix(wp_url: str, max_posts: int, skip: int = 0) -> list[dict]:
    page_size = 100
    affected: list[dict] = []
    page_num = 1
    while len(affected) < skip + max_posts and page_num <= 30:
        url = (
            f"{wp_url}/wp-json/wp/v2/posts"
            f"?per_page={page_size}&page={page_num}&orderby=date&order=desc"
            "&_fields=id,date,slug,link,title,featured_media,content"
        )
        log.info("REST page %d: %s", page_num, url[:120])
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            log.warning("REST page %d: status %d", page_num, r.status_code)
            break
        posts = r.json()
        if not posts:
            break
        affected.extend(p for p in posts if p.get("featured_media") == 0)
        page_num += 1
    log.info("Total afetados achados: %d (skip=%d, max=%d)", len(affected), skip, max_posts)
    return affected[skip:skip + max_posts]


def fetch_single_post(wp_url: str, post_id: int) -> Optional[dict]:
    url = (
        f"{wp_url}/wp-json/wp/v2/posts/{post_id}"
        "?_fields=id,date,slug,link,title,featured_media,content"
    )
    r = requests.get(url, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def extract_source_url(content_html: str) -> Optional[str]:
    soup = BeautifulSoup(content_html, "lxml")
    for p in reversed(soup.find_all("p")):
        text = p.get_text(" ", strip=True)
        if text.lower().startswith("fonte"):
            a = p.find("a", href=True)
            if a:
                return a["href"]
    for a in soup.find_all("a", href=True):
        if "sneakernews.com" in a["href"]:
            return a["href"]
    return None


def is_allowed_source(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return host in ALLOWED_DOMAINS
    except Exception:
        return False


# JS executado dentro da pagina (depois do Cloudflare resolver):
# 1. Coleta URLs de imagens (featured + galeria)
# 2. Pra cada uma, fetch + base64 + retorna lista
PAGE_JS = r"""
async (maxImgs) => {
  function normalize(url) {
    if (!url) return null;
    if (url.startsWith('//')) url = 'https:' + url;
    return url.split('?')[0];
  }

  const urls = [];
  const seen = new Set();
  // Featured
  const featured = document.querySelector('picture.article-featured-image img, .article-featured-image img');
  if (featured) {
    const src = featured.getAttribute('src') || featured.getAttribute('data-src');
    const norm = normalize(src);
    if (norm) { urls.push(norm); seen.add(norm); }
  }
  // Galeria
  const gallery = document.querySelectorAll('dt.gallery-icon img, .gallery-icon img');
  for (const img of gallery) {
    const src = img.getAttribute('src') || img.getAttribute('data-src');
    const norm = normalize(src);
    if (!norm || seen.has(norm)) continue;
    const low = norm.toLowerCase();
    if (['logo','avatar','gravatar','icon','advert','sponsor'].some(b => low.includes(b))) continue;
    seen.add(norm);
    urls.push(norm);
    if (urls.length >= maxImgs) break;
  }

  // Download cada uma via fetch (cookies de Cloudflare ja estao no contexto)
  const results = [];
  for (const url of urls) {
    try {
      const r = await fetch(url, { credentials: 'include', referrer: location.href });
      if (!r.ok) { results.push({ url, ok: false, status: r.status }); continue; }
      const buf = await r.arrayBuffer();
      const bytes = new Uint8Array(buf);
      let bin = '';
      for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
      results.push({
        url, ok: true,
        contentType: r.headers.get('content-type'),
        size: bytes.byteLength,
        b64: btoa(bin),
      });
    } catch (e) {
      results.push({ url, ok: false, error: String(e) });
    }
  }
  return results;
}
"""


def scrape_one_post(source_url: str, post_id: int, slug: str) -> list[dict]:
    """Retorna lista de dicts [{url, local_path, content_type, size}]."""
    holder: list = []

    def page_action(page):
        try:
            res = page.evaluate(PAGE_JS, MAX_IMAGES_PER_POST)
            holder.append(res)
        except Exception as e:
            log.warning("page_action exception: %s", e)
            holder.append([])

    log.info("[%d] fetching %s", post_id, source_url)
    t0 = time.time()
    try:
        page = StealthyFetcher.fetch(
            source_url,
            headless=True,
            network_idle=True,
            wait=2000,
            page_action=page_action,
        )
    except Exception as e:
        log.error("[%d] StealthyFetcher exception: %s", post_id, e)
        return []
    elapsed = time.time() - t0
    log.info("[%d] page status=%d (%.1fs)", post_id, page.status, elapsed)

    if not holder:
        log.warning("[%d] page_action nao foi executado", post_id)
        return []

    fetched = holder[0]
    if not fetched:
        log.warning("[%d] sem imagens fetchadas", post_id)
        return []

    post_dir = TMP_DIR / str(post_id)
    post_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    for i, entry in enumerate(fetched, 1):
        if not entry.get("ok"):
            log.warning("[%d]   #%d falhou: status=%s", post_id, i, entry.get("status"))
            continue
        b64 = entry.get("b64")
        if not b64:
            continue
        ctype = (entry.get("contentType") or "image/jpeg").split(";")[0].strip()
        ext = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/webp": ".webp", "image/gif": ".gif"}.get(ctype, ".jpg")
        fname = f"{slug}-{i:02d}{ext}"
        fpath = post_dir / fname
        try:
            data = base64.b64decode(b64)
            fpath.write_bytes(data)
        except Exception as e:
            log.warning("[%d]   #%d decode falhou: %s", post_id, i, e)
            continue
        saved.append({
            "url": entry["url"],
            "local_path": str(fpath.relative_to(ROOT)).replace("\\", "/"),
            "content_type": ctype,
            "size": len(data),
        })
        log.info("[%d]   #%d %s (%d bytes)", post_id, i, fname, len(data))
    return saved


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--single-post", type=int, default=None)
    p.add_argument("--max-posts", type=int, default=15)
    p.add_argument("--skip", type=int, default=0)
    p.add_argument("--output", type=str, default="scraped.json")
    return p.parse_args()


def main() -> int:
    setup_logging()
    load_dotenv(ROOT / ".env")
    wp_url = os.getenv("WP_URL", "").rstrip("/")
    wp_user = os.getenv("WP_USER", "")
    wp_pw = os.getenv("WP_APP_PASSWORD", "")

    log.info("Env check: WP_URL=%r, WP_USER=%r, WP_APP_PASSWORD=%s",
             wp_url, wp_user, "(set)" if wp_pw else "(EMPTY!)")

    if not wp_url:
        print("ERRO: WP_URL nao definido (env var ou .env)", file=sys.stderr)
        return 2

    args = parse_args()
    log.info("Args: max_posts=%d, skip=%d, single_post=%s, output=%s",
             args.max_posts, args.skip, args.single_post, args.output)
    TMP_DIR.mkdir(exist_ok=True)

    if args.single_post:
        post = fetch_single_post(wp_url, args.single_post)
        if not post:
            log.error("post %d nao encontrado", args.single_post)
            return 1
        if post.get("featured_media", 0) != 0:
            log.warning("post %d ja tem featured_media=%d", post["id"], post["featured_media"])
            return 0
        posts = [post]
    else:
        posts = fetch_posts_to_fix(wp_url, args.max_posts, args.skip)

    if not posts:
        log.info("Nenhum post pra processar.")
        return 0

    # Carrega scraped.json existente pra fazer merge
    out_path = ROOT / args.output
    if out_path.exists():
        with out_path.open("r", encoding="utf-8") as f:
            output = json.load(f)
    else:
        output = {}

    success_count = 0
    for i, post in enumerate(posts, 1):
        log.info("=== [%d/%d] post %d (%s) ===", i, len(posts), post["id"], post["slug"])
        src = extract_source_url(post.get("content", {}).get("rendered", ""))
        if not src:
            log.warning("[%d] sem backlink", post["id"])
            output[str(post["id"])] = []
            continue
        if not is_allowed_source(src):
            log.info("[%d] fonte fora do escopo: %s", post["id"], src)
            output[str(post["id"])] = []
            continue

        images = scrape_one_post(src, post["id"], post["slug"])
        output[str(post["id"])] = images
        if images:
            success_count += 1

        # Salva scraped.json incrementalmente (sobrevive a crash)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

    log.info("=== finalizou ===")
    log.info("Posts com imagens: %d / %d", success_count, len(posts))
    log.info("scraped.json: %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
