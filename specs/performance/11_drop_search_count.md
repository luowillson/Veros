# 11 — Drop the redundant `/search/count` endpoint

## Context

`api/app/routers/search.py` exposes `GET /search/count?q=`:

```python
@router.get("/count")
def search_count(db: DbSession, q: str = Query(default="")) -> dict[str, int]:
    return {"total": count_papers(db, q)}
```

For non-empty queries, `count_papers` calls `_query_candidate_ids`, which runs
the full search candidate pipeline — fuzzy text **and** the SBERT-based
`_semantic_candidate_ids`. So every hit on `/search/count` triggers a model
encode plus an HNSW ANN scan, just to return a number.

The endpoint is no longer needed. `/search/page` already returns
`{ results, total }` in a single round-trip (shipped in spec S6 alongside the
`Cache-Control` change), and the corresponding client helper
`fetchSearchCount` in `web/src/lib/api.ts` is **not called from anywhere** in
`web/src` — it is dead code held alive only by the route on the API side.

The empty-query path of `count_papers` (used internally by
`search_papers_with_total`) stays — it's the cheap `pg_class.reltuples`
lookup from spec S5 and has no SBERT cost.

## What to change

1. Delete the `/search/count` route and the now-unused `count_papers` import
   from `api/app/routers/search.py`.
2. Delete `fetchSearchCount` from `web/src/lib/api.ts`.
3. Remove the `/search/count?q=` row from the API table in `README.md` and
   the `/search/count` mention from `CODEBASE.md`.

`count_papers` itself stays in `api/app/services/search.py` — it is still
called by `search_papers_with_total` for the empty-query browse path.

## Implementation steps

1. `api/app/routers/search.py`:
   - Remove `count_papers` from the `from app.services.search import (...)`
     block.
   - Delete the `@router.get("/count")` handler.
2. `web/src/lib/api.ts`: delete the `fetchSearchCount` export.
3. `README.md`: drop the `/search/count?q=` row.
4. `CODEBASE.md`: drop `/search/count` from the search router comment.

## Verify

```bash
# 1. Endpoint is gone.
curl -s -o /dev/null -w "%{http_code}\n" \
  "http://localhost:8000/api/v1/search/count?q=transformer"
# expect: 404

# 2. /search/page still returns total — pagination still works.
curl -s "http://localhost:8000/api/v1/search/page?q=transformer&limit=5" \
  | jq '.total, (.results | length)'
# expect: a number, then 5

# 3. No remaining references.
rg -n "fetchSearchCount|/search/count|search_count" web/src api/app
# expect: no matches
```

## Risks / rollback

- This is a public-ish API surface. If any external caller (a script, an
  ingest tool, a dashboard) hits `/search/count`, they'll start getting 404s.
  Grep the repo before merging; if anything turns up outside `web/src/lib`
  and the docs, either keep the route or migrate the caller first.
- Rollback: re-add the route, the import, and the client helper. No schema
  or data impact.
