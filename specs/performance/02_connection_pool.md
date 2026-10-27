# 02 — Bump SQLAlchemy connection pool

## Context

`api/app/config.py` (look for `db_pool_size`, `db_max_overflow`,
`db_pool_timeout`) currently sets:

```python
db_pool_size: int = 10
db_max_overflow: int = 10
db_pool_timeout: int = 10
```

Total ceiling: **20 connections** shared across the FastAPI request handlers
*and* the Celery workers that import/score/analyse papers. Under any
concurrency this queues — a slow request blocks both other requests and
worker-driven ingest. With a 400k corpus the cost of ever waiting on the pool
shows up immediately as "the whole site feels slow."

`db_pool_timeout: int = 10` also means a starved client gives up after 10s
with `QueuePool limit … overflow … reached`.

## What to change

Raise the pool to a more realistic size for a multi-process deployment.
Typical Postgres can handle far more than 20 connections; the right ceiling
is bounded by Postgres' `max_connections`, not by the app.

Suggested values (tune to what GCP Postgres allows):

```python
db_pool_size: int = 20
db_max_overflow: int = 40
db_pool_timeout: int = 30
```

That's 60 max per API or worker process. If `max_connections` on the GCP
instance is small (e.g. 100), check it first — `SHOW max_connections;` —
and leave headroom for `psql`, the worker, etc.

## Implementation steps

1. Edit `api/app/config.py`:
   - `db_pool_size: int = 20`
   - `db_max_overflow: int = 40`
   - `db_pool_timeout: int = 30`
2. Confirm `api/app/db/session.py` actually passes these into
   `create_engine(...)` — it does today, but verify (look for
   `pool_size=settings.db_pool_size`).
3. Restart the API and Celery worker so the new engine is built.
4. Optional: also turn on `pool_pre_ping=True` in `create_engine` to drop
   stale connections automatically — useful for the GCP self-hosted Postgres
   where idle connections can be killed by network middleboxes.

## Verify

- `SHOW max_connections;` against the shared DB to confirm there's headroom.
- Run a small load test (e.g. `hey -n 200 -c 20 'http://localhost:8000/api/v1/search/page?q=&limit=20'`)
  and watch the API logs for `QueuePool limit … exhausted` — should not
  appear after the bump.
- `SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'veros%';`
  against the DB during load — should plateau below the new ceiling.

## Risks / rollback

- If `max_connections` on the DB is already tight, raising the pool can
  starve other clients. Check before merging.
- Reverting is just changing the three integers back. No schema impact.
