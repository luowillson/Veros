# 01 — Refresh planner stats with `ANALYZE`

## Context

After the 400k-row Semantic Scholar bulk import and migrations 0008–0010, the
Postgres planner's per-column statistics on `papers`, `veros_scores`, and
`ai_insights` are stale. Two concrete consequences:

1. The new browse-sort indexes (`papers_browse_score_idx` and the four
   per-dimension siblings created in `0008_paper_browse_columns.py`) may be
   ignored in favour of seq scans because the planner thinks the table is
   smaller than it is.
2. `count_papers` for empty-query browse (`api/app/services/search.py`)
   reads `pg_class.reltuples`. That value is only as accurate as the last
   `ANALYZE`. Right now it likely under-reports the corpus size by a lot.

This is a one-off DB maintenance step, not a code change.

## What to change

Run `ANALYZE` against the three tables that browse and search read from. No
schema change, no migration — just a SQL command against the shared DB.

## Implementation steps

1. Connect to the shared Postgres (the URL in `api/.env`'s `DATABASE_URL`).
2. Run:
   ```sql
   ANALYZE papers;
   ANALYZE veros_scores;
   ANALYZE ai_insights;
   ANALYZE paper_embeddings;
   ```
   `ANALYZE` is online — no lock on reads or writes — and on 400k rows takes
   tens of seconds.
3. Optionally also bump statistics targets on the columns the browse indexes
   sort on, so future autovacuum-driven analyses keep them sharp:
   ```sql
   ALTER TABLE papers ALTER COLUMN score SET STATISTICS 500;
   ALTER TABLE papers ALTER COLUMN ingested_at SET STATISTICS 500;
   ANALYZE papers;
   ```
   (Default is 100. Skip this if you want the smallest possible change.)

## Verify

```sql
-- Should now report ~400k, not the previous low number:
SELECT reltuples::bigint FROM pg_class WHERE relname = 'papers';

-- Should pick papers_browse_score_idx, not Seq Scan:
EXPLAIN ANALYZE
SELECT id FROM papers
ORDER BY score DESC NULLS LAST, ingested_at DESC NULLS LAST, created_at DESC
LIMIT 20;
```

In the app: hit `GET /api/v1/search/page?q=&limit=20` and confirm the response
`total` is in the right ballpark (it now comes from `reltuples`).

## Risks / rollback

None — `ANALYZE` only updates planner stats, no row data changes. There's
nothing to roll back.
