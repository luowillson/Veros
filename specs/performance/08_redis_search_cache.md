# 08 — Redis-backed result cache for repeat search queries

## Context

After the index work (specs S2–S5), search latency is bounded by the
`_query_candidate_ids` pipeline (≤50 IDs from fuzzy + semantic) and the
3-batch `build_results_for_ids` follow-up. That's ~50–200 ms per query
against Postgres at 400k rows — fast enough, but every search re-runs it.

Two patterns this is wasteful for:

- **Autocomplete / live search**: typing "diff", "diffu", "diffus", "diffusion"
  is 4 distinct queries with overlapping result sets, each issued from
  `fetchSearchLive` with `cache: "no-store"`.
- **Popular topics**: many users search the same handful of topics
  ("transformers", "rlhf", "in-context learning"). All hit Postgres cold.

In-process LRU (specs 04, 06) covers these inside one Uvicorn worker. Redis
gives you the same hit rate **across** workers and **across** API restarts,
and the infra is already in the stack (`redis_url` in `api/app/config.py`,
Celery is running on it).

This spec is more involved than 04/06 because cache invalidation on a
mutating corpus needs thought.

## What to change

Add a Redis-backed cache layer in front of `search_papers` /
`search_papers_with_total`, keyed by the full canonicalised query plus
parameters. Default TTL 5 minutes. Bypass for empty-query browse (already
covered by HTTP cache, spec S6) — caching it twice has no benefit.

Invalidate on ingest events, not on time alone, so newly-ingested papers
appear quickly.

## Implementation steps

1. **Pick a small Redis client interface.** Either:
   - Add `redis-py` as a dep (`pyproject.toml`) and create
     `app/services/cache.py` with `get_cache()` returning a singleton
     async/sync client that reads `settings.redis_url`.
   - Or reuse whatever Celery is already using — Celery wraps Redis but
     doesn't expose it cleanly. A separate `redis-py` client is simpler.

2. **Define a key shape** (write this down in `cache.py`):
   ```
   search:v1:{sort}:{mode}:{limit}:{offset}:{sha1(query_norm)}
   ```
   Versioning the prefix (`v1`) lets you invalidate everything by bumping the
   version when the response shape changes.

3. **Wrap `search_papers_with_total`:**
   ```python
   def cached_search_papers_with_total(db, query, *, limit, offset, mode, sort_by):
       q = query.strip()
       if not q:
           return search_papers_with_total(db, q, limit=limit, offset=offset, mode=mode, sort_by=sort_by)
       key = _build_key(q, limit, offset, mode, sort_by)
       cache = get_cache()
       hit = cache.get(key)
       if hit is not None:
           return _decode(hit)
       results, total = search_papers_with_total(db, q, limit=limit, offset=offset, mode=mode, sort_by=sort_by)
       cache.setex(key, 300, _encode((results, total)))
       return results, total
   ```
   Encoding: pickle is fine in-process; for safety prefer JSON of the
   pydantic dump (`PaperOut.model_dump_json()`).

4. **Wire the wrapper into the router** at
   `api/app/routers/search.py::search_page` and `search`. Keep the bypass
   for empty queries (HTTP cache handles those).

5. **Invalidate on ingest.** In `api/app/workers/tasks.py::ingest_paper_task`,
   at the end of a successful ingest, bump a generation key:
   ```python
   cache.incr("search:gen")
   ```
   And include `gen` in the cache key prefix. That gives O(1) invalidation
   of the whole search namespace without `KEYS *`.

6. **(Optional) Negative cache misses for `/search/lookup`'s "no
   OpenReview match" path** — the OpenReview API call there is the slowest
   thing in `lookup`. Cache "no match" for 1 hour to avoid re-hitting
   OpenReview for the same junk query.

## Verify

- Run a load test issuing the same query 10× in a row; the second through
  tenth should be sub-10 ms.
- Ingest a new paper, then re-run a search that should match it. Result
  appears (cache invalidated by `search:gen`).
- `redis-cli MONITOR` while paginating in the browser; confirm GETs and
  SETEXes are happening.
- Memory: `redis-cli INFO memory` — reasonable cache ceiling for 5-minute
  TTL on typical query volume should be well under 50 MB.

## Risks / rollback

- **Stale results** between an ingest and the cache invalidation. The
  `search:gen` bump fixes this for ingest-driven changes; for re-scoring
  (`compute_and_store_score`) and re-analysis (`analyze_paper`) you'd want
  to bump too if changes to score affect ranking. Be explicit about which
  events invalidate.
- **Pickling pydantic models** can break across pydantic versions. Use the
  JSON form via `model_dump_json` / `model_validate_json`.
- **Redis down-grade**: if Redis is unavailable, the wrapper should fall
  through to direct DB query, not error out. Wrap `cache.get` /
  `cache.setex` in try/except and log.
- **Privacy**: search results aren't user-specific in this app, so global
  Redis caching is safe. If `/saved` ever shows up in search, this stops
  being safe — re-evaluate then.
- Rollback: stop wrapping the calls; the cache keys age out on their own.
