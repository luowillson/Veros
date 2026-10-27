# 03 — `/stats` endpoint should use `pg_class.reltuples`, not `COUNT(*)`

## Context

`api/app/routers/health.py` exposes `GET /stats` for the landing page
`StatsFooter`:

```python
@router.get("/stats")
def stats(db: DbSession) -> dict[str, int]:
    paper_count: int = db.exec(select(func.count()).select_from(Paper)).one()
    review_count: int = db.exec(select(func.count()).select_from(Review)).one()
    return {"paper_count": paper_count, "review_count": review_count}
```

Both queries are `COUNT(*)` over the full tables. On 400k papers that's a few
hundred ms, every landing-page hit, with no caching. It's the same problem
`count_papers` had before spec S5 — and the same fix works.

The numbers in the footer are an order-of-magnitude indicator ("400k+ papers
analysed"). Exact accuracy isn't required.

## What to change

Replace each `COUNT(*)` with a `pg_class.reltuples` lookup, falling back to
`COUNT(*)` only when `reltuples` is missing or 0 (fresh table that has never
been analysed). Mirror the pattern already in
`api/app/services/search.py::count_papers`.

## Implementation steps

1. Edit `api/app/routers/health.py`:
   ```python
   from sqlalchemy import text as sa_text

   def _approx_row_count(db: DbSession, table: str) -> int:
       row = db.execute(
           sa_text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"),
           {"t": table},
       ).first()
       return int(row[0]) if row and row[0] is not None and int(row[0]) > 0 else -1

   @router.get("/stats")
   def stats(db: DbSession) -> dict[str, int]:
       paper_count = _approx_row_count(db, "papers")
       if paper_count < 0:
           paper_count = db.exec(select(func.count()).select_from(Paper)).one()
       review_count = _approx_row_count(db, "reviews")
       if review_count < 0:
           review_count = db.exec(select(func.count()).select_from(Review)).one()
       return {"paper_count": paper_count, "review_count": review_count}
   ```
2. Add `Cache-Control: public, max-age=60, stale-while-revalidate=600` on the
   response. Stats footer doesn't need second-by-second accuracy and shouldn't
   re-query Postgres for every page render.
3. (Optional) Move the response shape behind a small `StatsResponse`
   pydantic model if you want to make Cache-Control wiring cleaner via
   `Response`.

## Verify

```bash
time curl -s http://localhost:8000/api/v1/stats
# expect: well under 50 ms; numbers in the right ballpark
```

```sql
-- Sanity check the estimate is close enough:
SELECT
  reltuples::bigint AS approx_papers,
  (SELECT count(*) FROM papers) AS exact_papers
FROM pg_class WHERE relname = 'papers';
```

If the two numbers differ by more than ~10%, run `ANALYZE papers; ANALYZE
reviews;` (see spec 01).

## Risks / rollback

- If autovacuum has not run on a table since a big import, `reltuples` can
  be way off. Combine with spec 01 (`ANALYZE`) the first time.
- Rollback is the inverse of step 1. No schema impact.
