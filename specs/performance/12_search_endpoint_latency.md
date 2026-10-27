# 12 — `/search` and `/search/page?q=…` are 25× slower than their SQL

## Context

Found while verifying spec 04. Empty-query browse (`/search/page?q=`) is fine
(~500 ms). Non-empty `/search/page?q=diffusion` and `/search?q=diffusion` take
**100+ seconds end-to-end on a fully-isolated, no-contention call**, even
though the underlying SQL EXPLAINs at ~4 s and the per-stage in-process
profile sums to ~17 s.

Reproduced repeatedly:

- `EXPLAIN (ANALYZE, BUFFERS)` on the fuzzy candidate query (the dominant SQL
  in the path): **4.0 s** at threshold 0.16 with the current 4-arm BitmapOr.
- In-process profile of the same code path through SQLAlchemy:

  ```
  _fuzzy_text_candidate_ids:     15150 ms   (warm)
  _semantic_candidate_ids:         233 ms   (after spec 04 cache hit)
  _query_candidate_ids (sum):   116170 ms   ← 8× the sum of its parts
  build_results_for_ids:          1412 ms
  search_papers_with_total:     133937 ms
  ```

  `_query_candidate_ids` literally only calls fuzzy + semantic + dedupe.
  Something between the two sub-calls is adding ~100 s.

- HTTP `/search/page?q=diffusion` with `pg_stat_activity` clean before the
  call, no Celery worker, no other clients: **105–115 s** wall-clock.

When pile-up happens, every additional in-flight request adds another stuck
fuzzy query on the DB (Postgres keeps executing after the HTTP client
disconnects), and the system pseudo-deadlocks until queries are manually
cancelled.

## What is *not* the cause (already ruled out)

- Trgm GIN indexes — they're correctly built (`papers_title_trgm_lower`,
  `papers_abstract_trgm_lower`) and EXPLAIN shows a 4-arm BitmapOr using all
  of them. Verified with `\d papers` and `EXPLAIN (ANALYZE, BUFFERS)`.
- `_FUZZY_WORD_SIM_THRESHOLD = 0.16` — raising to 0.3 made things worse:
  the index brings back *more* candidate rows at higher thresholds in this
  corpus. 0.16 is a local optimum.
- Dropping the ILIKE OR-arms — *catastrophic*: planner abandons the trgm
  index for `<%` alone (selectivity estimate at 0.16 is too loose) and runs a
  Parallel Seq Scan in 42 s. The ILIKE OR-arms are load-bearing.
- Celery worker contention — reproduces with worker stopped.
- `build_results_for_ids` — already batched with `IN (...)`, no N+1, ~1.4 s.
- `pool_pre_ping=True` — costs ~one SELECT 1 per checkout (~60 ms RTT to
  GCP), nowhere near 100 s.
- Spec 04 LRU cache — works correctly in isolation; not on the slow path.

## Hypotheses worth investigating

1. **SQLAlchemy session/transaction state across queries.** All of
   `_set_word_similarity_threshold`, the fuzzy SELECT, the `<=>` pgvector
   SELECT, and the three `build_results_for_ids` SELECTs run in the same
   session/transaction. If something accumulates (savepoints? prepared
   statements? plan cache?) across multiple `set_config(...)` calls or `IN
   (...)` queries, that could explain the gap.
2. **The 4-arm BitmapOr's actual cost under sequential repeats.** The
   EXPLAIN was a single isolated psql run with warm GCP buffers. Re-running
   the same query within an open transaction may not benefit from the same
   buffer-cache state.
3. **Network behaviour to the GCP-hosted Postgres** (`100.89.186.45`).
   `EXPLAIN ANALYZE` runs server-side and reports 4 s. The 8,455 buffers it
   reads = ~66 MB; if those are streamed back to the client over Tailscale
   at slow speed, that's not in the EXPLAIN number. But this query only
   returns 50 IDs, not the heap pages — so this doesn't fit either.
4. **Something on the server side mutating between `_fuzzy_…` and the next
   call** — autovacuum kicking in mid-transaction, a background ANALYZE, or
   an incidental Celery retry that fires during testing.

## What to do

The diagnosis needs more instrumentation than I felt comfortable adding to
the WIP `api/app/services/search.py`:

1. Add timing logs (or `pg_stat_activity` snapshots from a separate
   connection) around each sub-call inside `_query_candidate_ids` and
   `search_papers_with_total`. Confirm whether the missing time is in
   `_fuzzy_text_candidate_ids`, `_semantic_candidate_ids`, the
   `_dedupe_ids` step, or session teardown.
2. With `DEBUG=true` in `api/.env`, capture a single slow `/search/page`
   call and compare wall-clock vs `pg_stat_statements` totals for the same
   PID.
3. Try the call from a Postgres *colocated* test client (e.g. a `psql` ssh'd
   onto the GCP VM) to remove the Tailscale link from the picture.
4. If 1–3 don't pin it down, bisect: temporarily replace
   `_fuzzy_text_candidate_ids` with `return []` and see if the endpoint
   returns in <1 s. Then replace `_query_candidate_ids` with
   `return _fuzzy_text_candidate_ids(db, q)` (drop semantic), etc.

## Verify

A fixed search path should give:

```bash
# After a `pg_stat_activity` confirms zero stuck queries:
time curl -s 'http://localhost:8000/api/v1/search/page?q=diffusion&limit=5'
# Target: well under 5 s on a warm cache; under 15 s cold.
```

And under load:

```bash
ab -n 50 -c 10 'http://127.0.0.1:8000/api/v1/search/page?q=diffusion&limit=5'
# Target: 0/50 failures, no stuck `lower(title) ILIKE` queries left in
# pg_stat_activity 60 s after the run completes.
```

## Risks / rollback

This ticket only investigates and instruments. Any fix it produces will be
its own change with its own rollback. Don't merge instrumentation logging
changes into main unless they're guarded by `DEBUG`.
