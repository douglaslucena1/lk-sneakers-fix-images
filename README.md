# LK Sneakers — Pipeline de correção de imagens

Pipeline pra restaurar imagens (featured + galeria `.lk-gallery`) em ~3000 posts do blog https://blog.lksneakers.com.br que perderam essas imagens por uma falha na automação de conteúdo a partir de 20/mar/2026.

Funciona standalone (Scrapling passa Cloudflare do sneakernews.com) e foi desenhado pra rodar em GitHub Actions a cada 6h, em chunks de ~50 posts/run.

## Stack

- **Python 3.12+**, `requests`, `beautifulsoup4`, `lxml`, `python-dotenv`, `Pillow`, `scrapling[fetchers]`
- **Scrapling StealthyFetcher** — passa Cloudflare turnstile do sneakernews.com (~35s/post)
- **WordPress REST API** — usa "Senha de aplicação" pro upload de mídia e update de posts
- **GitHub Actions** — runner Linux, Chromium pré-cacheado, cron de 6/6h

## Arquivos

| Arquivo | Função |
|---|---|
| `scrape.py` | Lista posts afetados + scrapeia imagens da fonte (sneakernews) + baixa local |
| `fix_missing_images.py` | Upload pra Media Library + update do post (featured + gallery prepended) |
| `decode_mcp_images.py` | (Apenas histórico do piloto) Utilitário pra decodificar JSONs do Playwright MCP |
| `requirements.txt` | Deps Python |
| `.env` | Credenciais (NÃO commitar) |
| `.env.example` | Template |
| `scraped.json` | Cache de URLs scrappeadas (paths locais + metadata) |
| `tmp/{post_id}/` | Imagens baixadas (regeneradas a cada run) |
| `reports/run_*.csv` | Audit trail por run |
| `logs/run_*.log` | Logs detalhados |
| `.github/workflows/fix-images.yml` | Workflow GHA |

## Pipeline (por post)

1. **REST `/wp/v2/posts`** com `featured_media=0` → posts a corrigir (top N por data desc)
2. **Extrai backlink** "Fonte: …" no `content.rendered` (regex/BS4)
3. **Filtra**: domínio em `ALLOWED_DOMAINS` (atualmente só `sneakernews.com`); else `skip`
4. **Scrapling StealthyFetcher** abre a fonte (passa Cloudflare turnstile ~35s)
5. **Dentro da página** (cookies de Cloudflare ativos), JS `evaluate`:
   - Coleta URLs de `picture.article-featured-image img` e `dt.gallery-icon img`
   - Pra cada uma, `fetch()` retorna bytes, encoda em base64
6. Decodifica base64 → salva em `tmp/{post_id}/{slug}-NN.jpg`
7. Upload pra `/wp/v2/media` com `Content-Disposition` + auth Basic (app password)
8. **Re-checa** `featured_media == 0` antes de update (idempotência)
9. **Build HTML** da galeria `.lk-gallery` (figure.lk-main + button.lk-thumb + script JS) + concat com `content.rendered` original
10. POST `/wp/v2/posts/{id}` com `featured_media` e `content`
11. Append linha CSV em `reports/run_*.csv`

## Setup (one-time)

### 1. Criar repo no GitHub

```bash
git init
git add .
git commit -m "initial"
git branch -M main
git remote add origin git@github.com:seu-user/lk-sneakers-fix-images.git
git push -u origin main
```

Recomendo **repo privado** (mas tem 2000 min/mês de Actions free, ~13 runs de 50 posts cada, ~650 posts/mês). Se preferir **repo público** (Actions ilimitado), avalie risco — todos os arquivos ficam visíveis (mas senha tá em Secrets, não no código).

### 2. Adicionar secrets no GitHub

Em **Settings → Secrets and variables → Actions → New repository secret**, adicione:

| Nome | Valor |
|---|---|
| `WP_URL` | `https://blog.lksneakers.com.br` |
| `WP_USER` | `lucena` |
| `WP_APP_PASSWORD` | (a senha de aplicação que você criou no WP) |

### 3. Habilitar Actions (se desabilitado)

Settings → Actions → General → Allow all actions

### 4. Primeiro run manual

Em **Actions → Fix Missing Images → Run workflow**:
- Branch: `main`
- `chunk_size`: `5` (pra testar)
- `dry_run`: `true` (pra não atualizar posts)
- Run

Verifique logs e artifacts. Se OK, refaça com `chunk_size=50` e `dry_run=false`.

### 5. Rodar em produção

A partir daí, o cron `0 */3 * * *` (a cada 3h, 8 runs/dia) roda automaticamente.
Default: chunk 100 posts/run × 8 runs/dia = ~800 posts/dia.
**Estimativa: ~4 dias pros 3000 posts** (com algum overlap/queue na prática).

Pra acelerar, dispare `workflow_dispatch` manualmente quantas vezes quiser entre os crons (limit: concurrency.fix-images garante 1 por vez na fila).

## Local (debug)

```bash
python -m venv .venv
.venv/Scripts/Activate.ps1   # Windows PowerShell
# ou: source .venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
scrapling install   # baixa Chromium

# .env preenchido (copia de .env.example)

# Smoke test
python scrape.py --single-post 13524     # 1 post
python fix_missing_images.py --single-post 13524 --scraped-json scraped.json --dry-run

# Batch
python scrape.py --max-posts 15
python fix_missing_images.py --scraped-json scraped.json
```

## Métricas medidas (no piloto)

- ~35-40s por post (Cloudflare resolution + scrape + download)
- ~80s por post (upload+update via REST com 5-8 imagens)
- **Total ~2 min/post** end-to-end
- **3000 posts ≈ 100h** spread em ~15 dias (4 runs de 50 posts/dia)

## Limitações conhecidas

- **Apenas sneakernews.com** está no escopo. Outras fontes (hypebeast, etc) ficam `skip` com `reason="source_out_of_scope"`. Pra adicionar, expandir `ALLOWED_DOMAINS` e adaptar selectors HTML.
- **Watermark "SNEAKER NEWS"** aparece em algumas imagens internas (era assim antes da automação parar — replicamos o comportamento).
- **Posts cuja fonte original foi deletada** (404 ou Cloudflare 403 persistente) ficam `skip` com `reason="no_images_scraped"` — aparecem no CSV pra revisão manual.

## Reverter / problemas

Cada run gera CSV em `reports/`. Se algo der errado:

```bash
# Listar posts atualizados na última run
grep success reports/run_*.csv | cut -d, -f1,2

# Reverter 1 post manualmente (Posts → Editar → remove featured + galeria → Update)
```

Backup do estado pré-pipeline está em `backup-blog-pre-redesign/` (não commitado, só local) — só inclui templates Elementor do redesign de header/footer (trabalho anterior).

---

**Suporte**: Se Cloudflare apertar mais ou Scrapling parar de passar, alternativas são:
- ScrapingBee/ScraperAPI (pagos, ~$30/mês pra 100k req)
- FlareSolverr self-hosted (free mas complexo)
- Atualizar Scrapling pra versão nova
