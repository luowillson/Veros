# 05 — Move landing-graph embedding backfill out of the request path; cache the result

## Context

`api/app/services/landing_graph.py::build_landing_graph` is called by
`GET /landing/graph` (router `api/app/routers/health.py`). On every cold hit
it does two expensive things synchronously:

1. **`_ensure_landing_graph_embeddings(db)`** (lines ~87–147) — runs a
   `COUNT(*)` over `paper_embeddings`, and if the count is below
   `_MIN_EMBEDDING_COUNT = 500` it issues a `LIMIT :missing` query that
   `LEFT JOIN`s `papers` ↔ `ai_insights` ↔ `veros_scores` ↔
   `paper_embeddings`, then **calls SBERT inline in batches of 64** for every
   missing row. With 400k papers this can compute hundreds of embeddings
   against the request thread before the page even starts rendering.

2. **`_fetch_topic_cluster`** does a vector neighbour query against
   `paper_embeddings` for `_TOPIC_NODE_COUNT = 1000` rows. After spec S7
   (HNSW), this is fast — but only after spec S7 is shipped.

`build_landing_graph` itself is also pure of the request: it picks a random
seed paper from the top 500 by score and returns a deterministic graph slice
for that seed. There is no per-user state.

Combined effect: the homepage can take seconds on a cold cache. Once the
embedding backfill has happened a few times the count exceeds 500 and the
backfill stops firing, but until then every cold visitor pays for it.

## What to change

Two independent moves:

**A. Move the embedding backfill to a Celery task.**

Replace the in-request call to `_ensure_landing_graph_embeddings` with a
short-circuit that just trusts what's there. Add a Celery beat task that
runs the backfill on a schedule (e.g. every 30 min) and on-demand when
ingestion runs. The request handler should never block on SBERT.

**B. Cache the landing-graph response.**

`build_landing_graph` produces a graph slice for a randomly-chosen seed.
Cache its output for 60 seconds (or longer — the homepage marketing graph
doesn't need novelty per request). Two acceptable implementations:

- **HTTP cache** — set `Cache-Control: public, max-age=60,
  stale-while-revalidate=600` on the response in
  `api/app/routers/health.py::landing_graph` and update the web client
  (`web/src/lib/api.ts`, `fetchLandingGraph` already uses
  `next: { revalidate: 600 }`) to honour the new max-age. Simplest, no extra
  infra.
- **In-memory cache** — wrap `build_landing_graph` in a small TTL cache
  (e.g. `cachetools.TTLCache(maxsize=1, ttl=60)`) so the same seed gets
  reused for a minute. Slightly more code but covers callers other than the
  HTTP route too.

Pick the HTTP-cache option unless you discover a non-HTTP caller.

## Implementation steps

1. **Spawn a backfill task.**
   - In `api/app/workers/tasks.py`, add:
     ```python
     @celery_app.task(name="landing_graph.backfill_embeddings")
     def backfill_landing_graph_embeddings_task() -> None:
         from app.db.session import get_session_context
         from app.services.landing_graph import _ensure_landing_graph_embeddings
         with get_session_context() as db:
             _ensure_landing_graph_embeddings(db)
     ```
     (Use whatever the existing pattern is for getting a Session inside a
     task — search `api/app/workers/tasks.py` for `with get_session` or
     similar; copy that.)
   - Also kick this task off as a follow-up at the end of
     `ingest_paper_task`, so newly-ingested papers get embedded shortly after.
   - Add a Celery beat schedule entry (30-minute cadence) in
     `api/app/workers/celery_app.py` (or wherever beat schedules live —
     grep for `beat_schedule`).

2. **Make `_ensure_landing_graph_embeddings` a no-op in the request path.**
   - In `build_landing_graph`, remove the call to
     `_ensure_landing_graph_embeddings(db)`. Replace with:
     ```python
     # Embeddings are backfilled by a Celery beat task — see
     # workers.tasks.backfill_landing_graph_embeddings_task. If the corpus
     # is genuinely empty here, return an empty graph rather than blocking
     # the request on SBERT.
     ```
   - If `_pick_seed_paper` returns `None` (no embeddings yet), the function
     already handles it by returning an empty graph — keep that path.

3. **Cache the response at the HTTP layer.**
   - In `api/app/routers/health.py::landing_graph`, take a `Response`
     parameter and set:
     ```python
     response.headers["Cache-Control"] = (
         "public, max-age=60, stale-while-revalidate=600"
     )
     ```
   - In `web/src/lib/api.ts::fetchLandingGraph`, update the `next: {
     revalidate: 600 }` to match (or leave higher — the existing 600 is
     fine as long as the SWR window aligns).

4. (Optional) Run the new beat task once manually to seed embeddings:
   ```bash
   cd api
   uv run python -c "from app.workers.tasks import backfill_landing_graph_embeddings_task; backfill_landing_graph_embeddings_task.delay()"
   ```

## Verify

- `time curl -s http://localhost:8000/api/v1/landing/graph >/dev/null` cold:
  should be sub-200 ms (was multi-second when backfill triggered).
- Hit it twice in quick succession — second hit should be a `304` or come
  from the browser/Next cache.
- Tail Celery worker logs while the beat task runs and confirm it logs
  "backfilling N missing embeddings."
- After the beat task has run a few cycles, `SELECT count(*) FROM
  paper_embeddings;` keeps growing without anyone hitting the homepage.

## Risks / rollback

- If the beat task isn't running (Celery beat not started), embedding count
  could stagnate. Document that `celery beat` is now required for the
  homepage to grow its embedding pool over time. Update README / Makefile
  accordingly.
- A request hitting the homepage before any embeddings exist will see an
  empty graph instead of an auto-populating one. That's already the
  fallback path for `_pick_seed_paper is None`, but flag it in the PR.
- Rollback: revert the change to `build_landing_graph` (re-call
  `_ensure_landing_graph_embeddings`) and remove the beat task. No schema
  impact.
