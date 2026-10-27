# 09 — Precomputed "trending papers" materialized view for the homepage

## Context

After spec S1 (denormalised browse columns + `papers_browse_score_idx`), the
empty-query first page of `/search/page` is fast: a top-N index scan on
`papers (score DESC, ingested_at DESC, created_at DESC)`. That's already
sub-100 ms.

This spec is a **further optimisation for the highest-traffic surface**: the
homepage / "discover" view, which is identical for every visitor and reads
the same first 20–60 papers all day. There are two places this currently
lives or will live:

- The empty-query browse (already cached via HTTP, spec S6).
- A future "trending" / "top this week" / "recently scored" surface that
  should rank by a recency-weighted score, not raw score alone.

Pure HTTP caching (spec S6) only helps after the first hit per cache window.
A materialized view shaves the first hit too, and gives you somewhere clean
to put a more interesting ranking expression than what's cheap to index.

This spec is **only worth doing** once the HTTP-cache miss latency on the
homepage shows up in observability. If browse first-page is already <100 ms
post-S1 and visitors aren't complaining, skip — adding refresh logic and
staleness has a cost too.

## What to change

Create a `paper_trending` materialized view with a stable ranked list of the
top ~500 papers under your chosen ranking expression. Index it. Refresh on a
schedule via Celery beat.

Wire `/search/page` to read from the materialized view when query is empty
**and** offset+limit fits inside the precomputed window; fall back to the
direct table query otherwise.

## Implementation steps

1. **Pick a ranking expression.** Examples:
   - Raw score: `score DESC NULLS LAST` (same as the current
     papers_browse_score_idx — no new info, skip).
   - Score + recency: `(score * 0.7 + recency_factor(ingested_at) * 0.3) DESC`
     where `recency_factor` is a simple decay from `ingested_at`.
   - Score + citation graph: `(score + log(1 + citations)) DESC` if you want
     to surface well-cited papers.

   Check `web/src/app/page.tsx` to see what "trending" should mean for the
   product before committing. The whole win here is in matching what the
   homepage actually wants to show.

2. **Migration `0011_paper_trending_mview.py`:**
   ```python
   def upgrade() -> None:
       op.execute("""
           CREATE MATERIALIZED VIEW paper_trending AS
           SELECT
             id,
             title,
             venue,
             score,
             novelty,
             technical,
             clarity,
             impact,
             ingested_at,
             created_at,
             (
               COALESCE(score, 0) * 0.7
               + GREATEST(0, 1 - EXTRACT(epoch FROM (now() - ingested_at)) / (86400.0 * 90)) * 3.0
             ) AS rank_score
           FROM papers
           WHERE score IS NOT NULL
           ORDER BY rank_score DESC NULLS LAST
           LIMIT 500
       """)
       op.execute("CREATE UNIQUE INDEX paper_trending_id_idx ON paper_trending(id)")
       op.execute("CREATE INDEX paper_trending_rank_idx ON paper_trending(rank_score DESC NULLS LAST)")
   ```
   The unique index on `id` enables `REFRESH MATERIALIZED VIEW CONCURRENTLY`,
   which doesn't lock readers.

3. **Refresh task** in `api/app/workers/tasks.py`:
   ```python
   @celery_app.task(name="paper_trending.refresh")
   def refresh_paper_trending_task() -> None:
       from app.db.session import get_session_context
       from sqlalchemy import text as sa_text
       with get_session_context() as db:
           db.execute(sa_text("REFRESH MATERIALIZED VIEW CONCURRENTLY paper_trending"))
           db.commit()
   ```
   Schedule via Celery beat every 15 minutes (or after `ingest_paper_task`
   completes — both is fine, the second is cheap).

4. **Read path** in `api/app/services/search.py`:
   ```python
   def _trending_candidate_ids(db: Session, limit: int, offset: int) -> list[str] | None:
       # Only handle the precomputed window; fall back otherwise.
       if offset + limit > 500:
           return None
       sql = sa_text(
           "SELECT id FROM paper_trending ORDER BY rank_score DESC NULLS LAST "
           "LIMIT :lim OFFSET :off"
       )
       return [r[0] for r in db.execute(sql, {"lim": limit, "off": offset}).fetchall()]
   ```
   In `search_papers` for the empty-query / `sort_by="score"` (or new
   `"trending"`) branch, try `_trending_candidate_ids` first; on `None`,
   fall back to `_browse_candidate_ids`.

5. (Optional) Add `"trending"` to `SortKey` if you want the homepage to opt
   into rank_score explicitly while regular browse keeps using raw score.
   This makes the surface intentional rather than implicit.

## Verify

```sql
EXPLAIN ANALYZE
SELECT id FROM paper_trending ORDER BY rank_score DESC NULLS LAST LIMIT 20;
-- expect: Index Scan using paper_trending_rank_idx, sub-millisecond
```

```bash
time curl -s 'http://localhost:8000/api/v1/search/page?q=&limit=20&sort=trending' >/dev/null
# expect: <50 ms cold, sub-millisecond on cache hit
```

Refresh by hand to confirm the path:
```bash
cd api
uv run python -c "from app.workers.tasks import refresh_paper_trending_task; refresh_paper_trending_task.delay()"
```
Then check that newly-ingested + scored papers appear in the view after the
next refresh cycle.

## Risks / rollback

- The materialized view is **stale by design** — that's the whole point.
  Refresh cadence + the ingest-triggered refresh keep it fresh "enough."
  Anything that needs real-time accuracy must use the direct query path.
- `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires the unique index — don't
  drop `paper_trending_id_idx`.
- Rollback: drop the view (and indexes), remove the read-path branch and
  the Celery task. No impact on other tables.
- This spec only pays off if the homepage / trending surface has measurable
  first-hit latency. Re-measure before merging — if HTTP cache already
  covers it, ship `paper_trending` only when the product wants
  recency-weighted ranking that the existing browse indexes can't express.
