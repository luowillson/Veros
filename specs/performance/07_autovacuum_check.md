# 07 — Audit and tune autovacuum after the 400k bulk import

## Context

The recent Semantic Scholar import added ~400k rows in one shot. Bulk imports
followed by ongoing trickle updates (scoring, analyse) are the worst case for
autovacuum: the table grows fast, then dead tuples accumulate from in-place
`UPDATE`s (e.g. `papers.score`, `papers.<dimension>` after spec 02
denormalisation, and `analyzed_at` updates from `analyze_paper`).

Symptoms of autovacuum falling behind:

- Sequential scans on `papers` get slower over time even though row count is
  flat.
- Index scans return more dead tuples than live, forcing extra heap fetches.
- `pg_class.reltuples` drifts away from real `count(*)`, breaking the spec
  03 / search.count optimisations.

This spec is an **investigation + tune**, not a fixed code change. The actions
below depend on what you find.

## What to change

Run the diagnostics. Based on the output, either leave defaults alone, or
tighten autovacuum settings on the hot tables.

## Implementation steps

1. **Diagnostics** — run against the shared Postgres:

   ```sql
   -- Dead-tuple ratio per hot table:
   SELECT
     relname,
     n_live_tup,
     n_dead_tup,
     CASE WHEN n_live_tup = 0 THEN 0
          ELSE round(n_dead_tup::numeric / n_live_tup, 3) END AS dead_ratio,
     last_autovacuum,
     last_autoanalyze,
     last_vacuum,
     last_analyze
   FROM pg_stat_user_tables
   WHERE relname IN ('papers', 'reviews', 'veros_scores', 'ai_insights', 'paper_embeddings')
   ORDER BY dead_ratio DESC;

   -- Bloat estimate (rough; uses pg_class):
   SELECT
     relname,
     pg_size_pretty(pg_relation_size(oid)) AS size,
     reltuples::bigint AS approx_rows
   FROM pg_class
   WHERE relname IN ('papers', 'reviews', 'veros_scores', 'ai_insights', 'paper_embeddings')
     AND relkind = 'r';
   ```

2. **Decision tree** based on results:

   - **Dead ratio < 0.1 and last_autovacuum is recent** → autovacuum is
     keeping up. Stop here. No code change.
   - **Dead ratio between 0.1 and 0.3** → tighten thresholds for the
     specific table:
     ```sql
     ALTER TABLE papers SET (
       autovacuum_vacuum_scale_factor = 0.05,   -- default 0.2
       autovacuum_analyze_scale_factor = 0.05,  -- default 0.1
       autovacuum_vacuum_cost_limit = 1000      -- default 200, faster cleanup
     );
     ```
     Apply to `veros_scores` and `ai_insights` too if their ratios are
     similar — both get hammered by the analyse pipeline.
   - **Dead ratio > 0.3** or `last_autovacuum` is days old → run a manual
     `VACUUM (VERBOSE, ANALYZE) papers;` to recover bloat, then tune
     thresholds as above.
   - **Index bloat** specifically (rare but possible after migrating indexes
     in 0009/0010) → check with the `pgstattuple` extension if available,
     otherwise `REINDEX INDEX CONCURRENTLY <name>;` on the suspect index.
     The HNSW index from spec 0010 doesn't bloat the same way btree/GIN do;
     it's mostly an issue for the trgm GIN indexes.

3. Document whatever you set in this file (append a "Findings on
   YYYY-MM-DD" section) so the next person knows what's in place.

## Verify

After tuning, re-run the diagnostics in step 1 a day later and check that
`last_autovacuum` is advancing and `dead_ratio` stays below ~0.1 on the hot
tables.

Also re-check spec 03: `pg_class.reltuples` should track real count within
~5%.

## Risks / rollback

- More aggressive autovacuum costs CPU and IO. On a small instance this can
  show up as latency spikes during vacuum runs. The
  `autovacuum_vacuum_cost_limit` knob is the main throttle.
- Storage-level options (`fillfactor`) are an option for `papers` if updates
  are very frequent, but only worth doing if `pgstattuple` confirms heavy
  bloat. Don't apply preemptively.
- Rollback is per-table:
  ```sql
  ALTER TABLE papers RESET (
    autovacuum_vacuum_scale_factor,
    autovacuum_analyze_scale_factor,
    autovacuum_vacuum_cost_limit
  );
  ```
