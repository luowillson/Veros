# Performance specs

Self-contained tickets to make Veros fast at 400k+ papers. Each file is a brief
a future Claude (or a human) can pick up cold, implement, and verify.

## How to use these

1. Pick a spec from the open list below.
2. Read it end-to-end before touching code — each one names the exact files,
   line ranges, and migration numbers to use, and gives an `EXPLAIN ANALYZE`
   or HTTP test to confirm the win.
3. File paths and line numbers were captured at the time the spec was written.
   If the line ranges drift, grep for the function names — the *function*
   identity is what matters, not the line.
4. Each spec is independently shippable. They can be combined into one PR
   when small (`#01`–`#03` are five-minute wins) or split when not (`#05`,
   `#06` touch real subsystems).
5. Always run the **Verify** section against the shared DB / running app
   before claiming done. "Code looks right" is not the bar.

## Already shipped

These are listed for context — do **not** redo them. If something here looks
broken when you start a new spec, that's a regression, not a TODO.

| # | What | Files |
|---|---|---|
| S1 | Cached browse-sort columns on `papers` (`score`, `novelty`, `technical`, `clarity`, `impact`) + 5 supporting indexes | `api/alembic/versions/0008_paper_browse_columns.py`, `api/app/db/models.py`, `api/app/services/dimensions.py` (`refresh_paper_browse_cache`), call-sites in `scoring.py` and `analyze.py` |
| S2 | `_browse_candidate_ids` rewritten as single-table indexed sort | `api/app/services/search.py` |
| S3 | Trgm GIN indexes rebuilt on `lower(title)` / `lower(COALESCE(abstract,''))` | `api/alembic/versions/0009_papers_trgm_lower.py` |
| S4 | Search candidate query uses `:q <% lower(col)` operator with `SET LOCAL pg_trgm.word_similarity_threshold` so the GIN trgm index actually fires | `_fuzzy_text_candidate_ids`, `top_title_similarity`, `best_title_match_id` in `api/app/services/search.py` |
| S5 | `count_papers` for empty-query browse uses `pg_class.reltuples` | `api/app/services/search.py` |
| S6 | `Cache-Control: public, max-age=30, stale-while-revalidate=300` on empty-query `/search/page` + matching `force-cache` on the web client | `api/app/routers/search.py`, `web/src/lib/api.ts` (`fetchSearchPage`) |
| S7 | `paper_embeddings` index switched IVFFLAT(lists=100) → HNSW(m=16, ef_construction=64), built `CONCURRENTLY` | `api/alembic/versions/0010_paper_embeddings_hnsw.py` |
| S8 | `ANALYZE` run on `papers`, `veros_scores`, `ai_insights`, `paper_embeddings`; statistics target on `papers.score` and `papers.ingested_at` bumped to 500 | shared DB (one-off), see `specs/performance/01_analyze_tables.md` |
| S9 | SQLAlchemy pool bumped 10/10/10 → 20/40/30 (`pool_size`/`max_overflow`/`pool_timeout`); `pool_pre_ping=True` already in place | `api/app/config.py`, see `specs/performance/02_connection_pool.md` |
| S10 | `/stats` uses `pg_class.reltuples` (with `COUNT(*)` fallback) and sets `Cache-Control: public, max-age=60, stale-while-revalidate=600` | `api/app/routers/health.py`, see `specs/performance/03_stats_reltuples.md` |
| S11 | SBERT query encode in `_semantic_candidate_ids` wrapped in a 1024-entry `lru_cache` keyed on `(model_name, normalized_query)`; in-process repeat hits return in ~500 ms (vs ~10 s cold including model load) | `api/app/services/search.py` (`_cached_query_embedding`, `_semantic_candidate_ids`), see `specs/performance/04_sbert_query_cache.md` |

## Open specs

Ordered roughly by impact-per-effort. Items in the same group are
complementary and good to ship together.

### Group B — Hot-path query/cost reduction

- **[11_drop_search_count.md](11_drop_search_count.md)** — delete the
  redundant `/search/count` endpoint (and its dead-code client helper) so
  every keystroke stops paying a SBERT encode
- **[12_search_endpoint_latency.md](12_search_endpoint_latency.md)** —
  `/search` and `/search/page?q=…` take 100+ s end-to-end even though the
  underlying SQL is 4 s on EXPLAIN; root cause unknown. Found while verifying
  spec 04
- **[05_landing_graph_async.md](05_landing_graph_async.md)** — move the
  synchronous embedding backfill out of the landing-graph request path and
  cache the response

### Group C — Bigger refactors

- **[06_pathways_explore_parallel.md](06_pathways_explore_parallel.md)** —
  parallelise / cache the 8 fan-out `search_papers` calls in
  `pathways.generate_explore_path`
- **[07_autovacuum_check.md](07_autovacuum_check.md)** — investigate
  autovacuum freshness after the 400k bulk import; tune if dead-tuple rate is
  high
- **[08_redis_search_cache.md](08_redis_search_cache.md)** — Redis-backed
  result cache for common autocomplete prefixes / repeat queries
- **[09_trending_papers_mview.md](09_trending_papers_mview.md)** —
  precomputed "top papers" materialized view for the homepage / first browse
  page

## Conventions for adding new specs here

- One spec per file, numbered `NN_short_slug.md`.
- Sections in order: **Context**, **What to change**, **Implementation
  steps**, **Verify**, **Risks / rollback**.
- Always name files and functions, not just behaviours. Future Claude reads
  this cold; "the search query" is ambiguous, `_fuzzy_text_candidate_ids` is
  not.
- If a spec depends on another, say so at the top.
