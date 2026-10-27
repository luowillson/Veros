# Veros

Veros surfaces and distills OpenReview peer reviews. Paste any OpenReview forum URL, get a deterministic **Veros Score (0-10)** plus AI-generated insights: a TL;DR, "read deeply" vs "skim or skip" sections, and verbatim reviewer voices.

---

## Prerequisites

| Tool | Version | Install |
|---|---|---|
| Docker Desktop | any recent | [docker.com](https://www.docker.com/products/docker-desktop/) |
| Node.js + pnpm | Node 20+, pnpm 9+ | `npm i -g pnpm` |
| Python | 3.12-3.13 | via `uv` below |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

---

## Quick start

### 1. Choose a database

For team development, use the shared Postgres database instead of syncing local
Docker volumes. Ask for the shared connection string, then put it in `api/.env`:

```text
DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:5432/<database>?sslmode=require
DEMO_USER_ID=<your-name>
DEMO_USER_EMAIL=<your-name>@veros.local
```

The shared database must be Postgres with `pgvector` available. Paper ingest,
scores, AI insights, and embeddings are then shared by everyone. Use a unique
`DEMO_USER_ID` so `/saved` stays personal.

If you are working offline or want an isolated database, run the local stack:

```bash
docker compose up -d
```

Postgres is exposed on `localhost:5432`, Redis on `localhost:6379`. Data persists in a Docker volume (`pgdata`).

Redis can stay local even when Postgres is shared; it is only the Celery queue:

```bash
docker compose up -d redis
```

### 2. Set up the API

```bash
cd api
cp .env.example .env    # fill in API keys and, for team dev, the shared DATABASE_URL
uv sync                 # create venv and install all Python deps
uv run alembic upgrade head   # create tables + pgvector/pg_trgm extensions
```

Start the API server (hot-reload):

```bash
uv run uvicorn app.main:app --reload
# http://localhost:8000
# http://localhost:8000/docs  (Swagger UI)
```

### 3. Start the Celery worker

Open a second terminal in `api/`:

```bash
uv run celery -A app.workers.celery_app:celery_app worker --loglevel=info
```

The worker handles ingest, LLM analysis, and embedding tasks triggered when you visit an unknown paper URL.

> On macOS, the worker is configured to use Celery's `solo` pool automatically.
> This avoids `SIGABRT` crashes from native ML dependencies such as
> `sentence-transformers` / `torch` inside prefork worker processes.

### 4. Start the web app

```bash
cd web
pnpm install
pnpm dev
# http://localhost:3000
```

Open the frontend at `http://localhost:3000` during local development. The API
also allows `http://127.0.0.1:3000` for convenience, but `localhost` is the
canonical URL used in examples.

---

## Ingesting your first paper

The easiest way: visit a paper page directly using a real OpenReview forum ID. For example, this ICLR 2024 paper on sparse autoencoders:

```text
http://localhost:3000/papers/F76bwRSLeK
```

If the paper isn't in the database the API returns 202, the Celery worker fetches reviews from OpenReview, scores the paper, runs LLM analysis, and the page transitions from skeleton to full view automatically.

**Using the search box:** paste any OpenReview forum URL or forum ID into the landing page search. If the paper is already indexed it appears in results; if not, go to `/papers/<id>` to trigger ingestion.

**Via curl:**

```bash
curl -X POST http://localhost:8000/api/v1/papers/F76bwRSLeK/ingest
```

## Bulk fetch papers from OpenReview

Use this when you want to fetch a whole OpenReview venue. Keep the OpenReview
fetch separate from Postgres: first write a local JSONL file, then import that
file into the database.

Fetch a small local sample first:

```bash
cd api
uv run python scripts/fetch_openreview_venue_jsonl.py \
  --venue ICLR.cc/2025/Conference \
  --decision accepted \
  --limit 5 \
  --output ../data/iclr_2025_accepted_reviews.jsonl
```

If that looks good, remove `--limit` to fetch the full accepted venue:

```bash
uv run python scripts/fetch_openreview_venue_jsonl.py \
  --venue ICLR.cc/2025/Conference \
  --decision accepted \
  --output ../data/iclr_2025_accepted_reviews.jsonl
```

For ICLR 2026, use the same two-step flow:

```bash
cd api
uv run python scripts/fetch_openreview_venue_jsonl.py \
  --venue ICLR.cc/2026/Conference \
  --decision accepted \
  --delay 0.5 \
  --output ../data/iclr_2026_accepted_reviews.jsonl
```

The fetcher is resumable. If it is interrupted, rerun the same command and rows
already present in the local JSONL file will be skipped. Use `--decision all` if
you want every submission rather than only accepted papers.

Then import the local file into Postgres:

```bash
uv run python scripts/import_openreview_jsonl.py \
  --source ../data/iclr_2025_accepted_reviews.jsonl
```

The import step bulk-uploads papers and reviews, skips existing papers by
default, and does not compute scores unless you pass `--score`. Add `--force`
only when you want to refresh existing database rows.

For hosted Supabase/Postgres, prefer a moderate chunk size and a small pause
between transactions:

```bash
uv run python scripts/import_openreview_jsonl.py \
  --source ../data/iclr_2026_accepted_reviews.jsonl \
  --chunk-size 250 \
  --chunk-delay 0.25 \
  --score \
  --score-delay 0.02
```

---

## Creating a local database from repo data

The live Postgres database is local machine state and is not pushed to GitHub. The repo does include the source data needed to recreate it locally, including `data/neurips_2025_accepted_reviews.jsonl`, `paper_scores.json`, and `score_scales.json`.

For a fresh clone, each developer should create their own local database:

```bash
# 1. Start Postgres + Redis from the repo root
docker compose up -d

# 2. Create API env + install dependencies
cd api
cp .env.example .env
uv sync

# 3. Create database tables and extensions
uv run alembic upgrade head

# 4. Import the tracked NeurIPS dataset into Postgres
uv run python scripts/import_neurips_2025.py \
  --source ../data/neurips_2025_accepted_reviews.jsonl
```

After import, the website can serve the stored papers directly from Postgres without re-scraping OpenReview.

To test a small sample first:

```bash
uv run python scripts/import_neurips_2025.py \
  --source ../data/neurips_2025_accepted_reviews.jsonl \
  --limit 5
```

The importer is safe to rerun. It upserts papers, reviews, and scores by ID.
By default, it skips papers that already exist in the database. To force a
refresh of existing rows, pass `--force`.

---

## OpenReview scoring utilities

This repo also includes local scoring tools for OpenReview review data. They can fetch reviews, normalize venue-specific scores, cache score summaries, and bulk-export accepted-paper review data.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### CLI usage

Fetch full reviews:

```bash
python openreview_reviews.py <paper_id> --format markdown --output reviews.md
```

Search by paper title within a conference and print score fields:

```bash
python openreview_reviews.py \
  --title "Optimal Mistake Bounds for Transductive Online Learning" \
  --conference "NeurIPS.cc/2025/Conference" \
  --scores-only
```

Add venue scoring scales:

```bash
python openreview_reviews.py \
  --add-score-scales NeurIPS.cc/2025/Conference \
  rating=6 quality=4 clarity=4 significance=4 originality=4
```

Backfill the local score cache from generated Markdown files:

```bash
python openreview_reviews.py --cache-parsed-scores reviews.md reviews2.md
```

Parse every accepted NeurIPS 2025 paper and its reviews into JSONL:

```bash
python scripts/parse_neurips_2025_accepted.py
```

The bulk parser sleeps `0.5` seconds between paper requests by default to reduce rate-limit risk. For a more conservative run:

```bash
python scripts/parse_neurips_2025_accepted.py --delay 1.0
```

Test the bulk parser on a small sample first:

```bash
python scripts/parse_neurips_2025_accepted.py --limit 5
```

### Backend integration

The reusable service API for the standalone tooling lives in `scoring.service`:

```python
from scoring.service import get_score_summary

payload = get_score_summary(
    title="Optimal Mistake Bounds for Transductive Online Learning",
    conference="NeurIPS.cc/2025/Conference",
    use_cache=True,
)
```

The returned payload is JSON-safe and can be sent directly from a Flask, FastAPI, or other backend route to a frontend.

---

## Environment variables (`api/.env`)

```text
# Local Docker Postgres. For shared team dev, replace with the hosted pgvector
# Postgres URL from api/shared-db.env.example.
DATABASE_URL=postgresql+psycopg://veros:veros@localhost:5432/veros
REDIS_URL=redis://localhost:6379/0

# LLM provider
LLM_PROVIDER=gemini

# Gemini (OpenAI-compatible mode)
GEMINI_API_KEY=<your key from aistudio.google.com>
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
GEMINI_MODEL=gemini-3-flash-preview

# OpenReview credentials, only needed for auth-gated venues
OPENREVIEW_USERNAME=
OPENREVIEW_PASSWORD=

EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
# Use per-developer values when connecting to the shared database.
DEMO_USER_ID=demo-user
DEMO_USER_EMAIL=demo@veros.local
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
LOG_LEVEL=INFO
```

`api/shared-db.env.example` contains a smaller template for joining the shared
team database.

Useful root commands:

```bash
make infra-up     # local Postgres + Redis
make redis-up     # local Redis only, for shared Postgres mode
make db-migrate   # cd api && uv run alembic upgrade head
make db-merge-to-shared
make api-dev
make worker
make web-dev
```

To merge an existing local Docker database into the shared team database, make
sure `api/.env` points at the shared `DATABASE_URL`, then run:

```bash
make db-merge-to-shared
```

The merge script upserts paper data in dependency order. For a teammate whose
local saved papers are still under `demo-user`, run from `api/` with:

```bash
uv run python scripts/merge_db_to_shared.py --rewrite-saved-user-id <teammate-name>
```

Use `--dry-run` first to preview row counts without writing.

`web/.env.local`:

```text
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
```

---

## API endpoints

Base: `http://localhost:8000/api/v1`

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/stats` | Paper + review counts |
| GET | `/landing/graph` | Cached semantic graph used on the landing page |
| GET | `/search?q=&limit=&offset=&sort=&mode=` | Text + semantic search |
| GET | `/search/page` | Same as `/search`, plus a `total` count for pagination |
| POST | `/search/lookup` | Submit-time intent classifier; pulls a missing paper from OpenReview when needed |
| GET | `/papers/{id}` | Full paper detail; 202 + enqueue if not ingested |
| GET | `/papers/{id}/status` | `{ingest, analysis}` status |
| POST | `/papers/{id}/ingest` | Synchronous ingest |
| POST | `/papers/{id}/analyze` | Re-run LLM analysis |
| POST | `/papers/batch` | Fetch many papers by id in one query |
| POST | `/pathways/from-paper/{id}` | Build or reuse a cached learning pathway for one paper |
| POST | `/pathways/from-topic` | Build or reuse a cached learning pathway for a topic |
| POST | `/pathways/explore` | Topic-driven explore path used by the `/explore` page |
| POST | `/pathways/explore/order` | LLM-ordered local explore candidates |
| GET | `/pathways/{id}` | Fetch a previously generated learning pathway |
| GET | `/rankings/authors` | Author leaderboard by average Veros score |
| GET | `/saved` | Demo user's reading list |
| GET | `/saved/{id}` | Whether the paper is saved by the current user |
| POST | `/saved` | Save a paper `{paper_id}` |
| DELETE | `/saved/{id}` | Unsave a paper |

Interactive docs are available at `http://localhost:8000/docs`.

---

## Learning pathways (MVP)

The MVP pathway feature is local-first:

- it searches only the already-ingested local corpus
- uses the LLM once to infer conceptual learning stages
- retrieves local papers separately for each stage
- ranks candidates using similarity, anchor concepts, Veros score, and clarity
- caches the generated pathway in Postgres for reuse
- marks weak or missing stages as `pending_enrichment`
- enqueues a bounded background OpenReview enrichment job for weak stages

Create a pathway from a seed paper:

```bash
curl -X POST http://localhost:8000/api/v1/pathways/from-paper/F76bwRSLeK
```

Create a pathway from a topic:

```bash
curl -X POST http://localhost:8000/api/v1/pathways/from-topic \
  -H "Content-Type: application/json" \
  -d '{"topic":"sparse autoencoders for language models","limit":6}'
```

By default, repeated requests reuse a cached pathway for the same user and
seed. To force regeneration while testing, add `?force=true`:

```bash
curl -X POST "http://localhost:8000/api/v1/pathways/from-paper/F76bwRSLeK?force=true"
```

When a pathway has broad weak coverage from the local corpus, the response may
return `status: "pending_enrichment"` and include per-stage `match_quality`,
`search_query`, and `anchor_concepts`. By default, Veros only escalates to
background OpenReview enrichment when at least two stages are weak or missing,
or when fewer than two stages are strong. A background Celery job then searches
a small set of OpenReview venues for candidate papers, ingests any strong
matches it finds, and regenerates the pathway.

This MVP does not live-search the web. If the local corpus is too sparse, the
endpoint returns an error instead of scraping external sources inline.

---

## Switching LLM providers

Edit `api/.env`:

```text
LLM_PROVIDER=gemini
LLM_PROVIDER=zai
```

Both use an OpenAI-compatible HTTP interface. Adding a new provider requires implementing one method in `api/app/services/llm/provider.py` and registering it in `factory.py`.

The current default in `api/app/config.py` is:

```text
LLM_PROVIDER=gemini
GEMINI_MODEL=gemini-3-flash-preview
```

---

## Pages

| URL | Description |
|---|---|
| `/` | Landing page with search box, live stats, and semantic graph |
| `/search?q=` | Results grid (paginated) |
| `/papers/{id}` | Full paper view (with ingest pending state) |
| `/saved` | Reading list |
| `/explore?q=` | Learning-pathway view for a topic |
| `/ranking` | Author leaderboard ranked by average Veros score |
| `/ranking/worst` | Same leaderboard, ranked from lowest score |
| `/ranking/search` | Author-name search inside the ranking view |

---

## Re-embedding already-ingested papers

After a fresh ingest the embedding task is queued automatically. To manually embed a paper that was ingested before the worker was running:

```bash
cd api
uv run celery -A app.workers.celery_app:celery_app call \
  veros.embed_paper --args='["F76bwRSLeK"]'
```
