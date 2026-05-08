"""
decode_mcp_images.py - Le arquivos JSON gerados pelo MCP (com b64 das imagens),
decodifica, salva como JPG em tmp/{post_id}/, e gera/atualiza scraped.json.

Uso:
  python decode_mcp_images.py --post-id 13538 --slug puma-stewie-5-tenis-de-breanna-stewart \\
      --json img-test.json --json img-13538-rest.json
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
TMP_DIR = ROOT / "tmp"
SCRAPED_JSON = ROOT / "scraped.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--post-id", type=int, required=True)
    p.add_argument("--slug", type=str, required=True)
    p.add_argument("--json", action="append", required=True,
                   help="caminho pra arquivo JSON do MCP (pode repetir)")
    p.add_argument("--output", type=str, default="scraped.json")
    return p.parse_args()


def extract_results(payload: dict | list) -> list[dict]:
    """O MCP serializa como {result: ...} ou similar. Normaliza."""
    # Casos:
    #  1. payload = {"ok":true,"size":...,"b64":"..."}              => 1 imagem
    #  2. payload = {"count":N,"results":[{...}, ...]}              => N imagens
    if isinstance(payload, dict):
        if "results" in payload and isinstance(payload["results"], list):
            return payload["results"]
        if "b64" in payload and "url" not in payload:
            # Single sem url; retornamos um item sem url (decode-only)
            return [payload]
        if "b64" in payload:
            return [payload]
    if isinstance(payload, list):
        return payload
    return []


def main() -> int:
    args = parse_args()
    TMP_DIR.mkdir(exist_ok=True)
    post_dir = TMP_DIR / str(args.post_id)
    post_dir.mkdir(exist_ok=True)

    all_entries: list[dict] = []
    for jp in args.json:
        path = Path(jp)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            print(f"AVISO: {path} nao existe, pulando", file=sys.stderr)
            continue
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        entries = extract_results(payload)
        all_entries.extend(entries)

    if not all_entries:
        print("Nenhuma imagem nos JSONs fornecidos.", file=sys.stderr)
        return 1

    saved: list[dict] = []
    seen_urls: set[str] = set()
    idx = 0
    for entry in all_entries:
        if not entry.get("ok", True):
            print(f"  skip (not ok): {entry.get('url') or entry}", file=sys.stderr)
            continue
        b64 = entry.get("b64") or entry.get("data")
        if not b64:
            print(f"  skip (no b64): {entry.get('url') or entry}", file=sys.stderr)
            continue
        url = entry.get("url") or "unknown"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        idx += 1

        ctype = (entry.get("contentType") or "image/jpeg").split(";")[0].strip()
        ext = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/webp": ".webp", "image/gif": ".gif"}.get(ctype, ".jpg")
        fname = f"{args.slug}-{idx:02d}{ext}"
        fpath = post_dir / fname
        try:
            data = base64.b64decode(b64)
        except Exception as e:
            print(f"  decode falhou {url}: {e}", file=sys.stderr)
            continue
        fpath.write_bytes(data)
        saved.append({
            "url": url,
            "local_path": str(fpath.relative_to(ROOT)).replace("\\", "/"),
            "content_type": ctype,
            "size": len(data),
        })
        print(f"  saved {fname} ({len(data)} bytes)")

    # Merge no scraped.json existente
    out_path = ROOT / args.output
    if out_path.exists():
        with out_path.open("r", encoding="utf-8") as f:
            existing = json.load(f)
    else:
        existing = {}
    existing[str(args.post_id)] = saved

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    print(f"\nOK: {len(saved)} imagens salvas em {post_dir}")
    print(f"OK: {out_path} atualizado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
