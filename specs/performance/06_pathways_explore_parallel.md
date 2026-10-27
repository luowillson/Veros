# 06 — Parallelise / cache `/explore`'s 8× `search_papers` fan-out

## Context

`web/src/components/explore/ExploreView.tsx` calls
`/api/v1/pathways/explore`, which routes to
`api/app/services/pathways.py::generate_explore_path`. That function fans out
8 calls to `search_papers(...)` (look around lines ~599 and ~617 for the
loops over sub-topics, with `limit=120`). Each call is sequential and each
hits the full search pipeline:

- `_fuzzy_text_candidate_ids` (now indexed — fast after specs S3+S4)
- `_semantic_candidate_ids` (one SBERT encode per call — see spec 04)
- `build_results_for_ids` (3 batched queries: `papers`, `veros_scores`,
  `ai_insights`)

So a single explore request issues 8× sequential pipelines. Even at
~50 ms each that's 400+ ms baseline; with embedding encode it's worse.

The 8 calls are **independent of each other** — different sub-topic strings,
no shared state — so they're embarrassingly parallel.

## What to change

Two changes, additive:

**A. Run the 8 `search_papers` calls concurrently.**

The most ergonomic option in FastAPI is `asyncio.to_thread` since
`search_papers` is sync (it talks to a sync SQLAlchemy session). But beware:
**each thread needs its own DB session**. Don't share the `db` parameter
across threads — SQLAlchemy sessions are not thread-safe.

The cleanest pattern: open a fresh `Session` inside each task, hand it to
`search_papers`, close on exit. Use `app.db.session.SessionLocal()` (or
whatever the factory is — grep `api/app/db/session.py`).

```python
import asyncio
from app.db.session import SessionLocal

async def _run_one(query: str, limit: int, ...) -> list[PaperOut]:
    def _work() -> list[PaperOut]:
        with SessionLocal() as db:
            return search_papers(db, query, limit=limit, ...)
    return await asyncio.to_thread(_work)

results = await asyncio.gather(*[_run_one(q, ...) for q in sub_topic_queries])
```

If `generate_explore_path` is currently sync, either make the entry-point
async or use `concurrent.futures.ThreadPoolExecutor(max_workers=8)` directly.
The threading version is fine — the spec just needs the 8 calls to overlap.

**B. Cache the result by sub-topic.**

Sub-topic strings are stable across users for the same root topic. Wrap
each per-sub-topic search in either:

- An in-process LRU keyed by `(query, limit, mode, sort)` — same shape as
  spec 04; size 256 entries is fine.
- Or Redis with a 5-minute TTL — better if you have multiple workers (see
  spec 08 for the broader Redis cache).

Start with the LRU. If hit rates are low, escalate to Redis.

**Watch out:** the connection pool (spec 02) needs to be high enough to
handle 8 concurrent sessions per request × however many concurrent explore
requests you expect. Bump that first.

## Implementation steps

1. Audit `api/app/services/pathways.py::generate_explore_path` — confirm
   the 8-call fan-out is what's there. Find both the sub-topic-generation
   step and the per-sub-topic search loop. Note the actual function
   signatures (the file has shifted since the original survey).
2. Refactor the search loop into a helper:
   ```python
   def _search_one(query: str, *, limit: int, mode: SearchMode, sort_by: SortKey) -> list[PaperOut]:
       with SessionLocal() as db:
           return search_papers(db, query, limit=limit, mode=mode, sort_by=sort_by)
   ```
3. Replace the loop with an `asyncio.gather` over `asyncio.to_thread(_search_one, ...)`.
   If the calling function is currently sync, you have two options:
   - Make `generate_explore_path` `async` and update its callers (search the
     codebase: `git grep generate_explore_path`).
   - Use `concurrent.futures.ThreadPoolExecutor(max_workers=8)` directly
     and stay sync. Simpler, works.
4. Add an LRU cache around `_search_one`:
   ```python
   @lru_cache(maxsize=256)
   def _cached_search_one(query: str, limit: int, mode: str, sort_by: str) -> tuple[PaperOut, ...]:
       with SessionLocal() as db:
           return tuple(search_papers(db, query, limit=limit, mode=cast(SearchMode, mode), sort_by=cast(SortKey, sort_by)))
   ```
   Tuples for hashability; convert back to list at the call site.
5. Make sure the connection pool can sustain 8 concurrent sessions per
   request. Check spec 02; bump first if not done.

## Verify

```bash
# Cold hit: parallel, no cache hits
time curl -s -X POST http://localhost:8000/api/v1/pathways/explore \
  -H 'content-type: application/json' \
  -d '{"topic": "diffusion models"}' >/dev/null

# Warm hit: same payload, should be much faster (LRU hits)
time curl -s -X POST http://localhost:8000/api/v1/pathways/explore \
  -H 'content-type: application/json' \
  -d '{"topic": "diffusion models"}' >/dev/null
```

Cold should be roughly the time of one `search_papers` call (not 8). Warm
should be DB-only fast.

Look at Postgres `pg_stat_activity` while a request runs — should see
multiple concurrent connections from one process, not one at a time.

## Risks / rollback

- **Connection-pool exhaustion** is the main risk. 8 concurrent sessions ×
  N concurrent explore requests can blow past `db_pool_size +
  db_max_overflow`. Land spec 02 first.
- Threading SQLAlchemy sessions is fine **only** when each thread opens its
  own. Don't share `db` across `to_thread` calls — that's the most common
  bug here.
- LRU cache means stale results possible if a user re-queries right after
  ingestion changes the corpus. 5-minute TTL via Redis (spec 08) is the
  upgrade path. For now, the LRU is bounded by process lifetime.
- Rollback: revert to the sequential loop. No schema impact.
