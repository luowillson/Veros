# 04 — LRU-cache the SBERT query encode

## Context

`api/app/services/search.py::_semantic_candidate_ids` runs the
sentence-transformer model on the **query string** every time `/search` is
called:

```python
provider = get_embedding_provider()
embedding = provider.encode([q])[0]
```

This is a CPU/IO cost per request, paid before the DB even runs. Common
queries ("diffusion", "transformers", "rlhf") get encoded over and over for
identical vectors. The encode itself is cheap on a laptop (~30–80 ms for
all-MiniLM-L6-v2) but expensive enough to dominate small searches and add up
under load.

The query embedding is a deterministic function of the (model, normalized
query string), so it caches trivially.

## What to change

Wrap the encode in a small in-process LRU keyed by `(model_name,
normalized_query)`. Cache hits return the existing 384-dim list without
touching the model. Cache size around 1024 entries is plenty — that's ~3 MB
of float32 vectors and covers any reasonable working set.

The sentence-transformer model is loaded once per process via
`get_embedding_provider()`, and an LRU at module level inherits that lifetime,
so this is safe across repeated requests within a worker.

## Implementation steps

1. Add a helper near the top of `api/app/services/search.py` (or in
   `api/app/services/embeddings/factory.py` if you'd rather keep it next to
   the provider):

   ```python
   from functools import lru_cache

   @lru_cache(maxsize=1024)
   def _cached_query_embedding(model_name: str, q_norm: str) -> tuple[float, ...]:
       provider = get_embedding_provider()
       return tuple(provider.encode([q_norm])[0])
   ```

   Tuple (not list) so it's hashable and re-cacheable cleanly. Convert back
   to list at the call site if needed.

2. In `_semantic_candidate_ids`:
   ```python
   from app.config import get_settings
   ...
   def _semantic_candidate_ids(db: Session, q: str) -> list[str]:
       qn = q.strip().lower()
       if not qn or not _has_embeddings(db):
           return []
       model_name = get_settings().embedding_model
       embedding = list(_cached_query_embedding(model_name, qn))
       vec_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"
       sql = sa_text(
           "SELECT paper_id FROM paper_embeddings "
           "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :n"
       )
       rows = db.execute(sql, {"vec": vec_str, "n": _CANDIDATE_POOL}).fetchall()
       return [r[0] for r in rows]
   ```

3. Normalisation: lowercase + collapsed whitespace is enough. Don't
   over-normalise (e.g. don't strip stopwords) — that would change which
   queries collide and could hurt recall.

4. Cache invalidation: when `LLM_PROVIDER` / `embedding_model` changes, the
   cache is keyed by `model_name`, so old vectors won't be returned for the
   new model. The only way to leak across runs is editing the encode
   implementation without bumping the model name; that's a developer choice
   and doesn't need a runtime hook.

## Verify

```bash
# Prime the cache:
curl -s 'http://localhost:8000/api/v1/search?q=diffusion&limit=5' >/dev/null

# Repeated identical query — second call should be measurably faster than
# the first because no encode happens.
time curl -s 'http://localhost:8000/api/v1/search?q=diffusion&limit=5' >/dev/null
time curl -s 'http://localhost:8000/api/v1/search?q=diffusion&limit=5' >/dev/null
```

Optional: add a debug log on cache miss only and confirm it doesn't fire on
repeats.

## Risks / rollback

- LRU lives in-process. With multiple Uvicorn workers each gets its own —
  fine for a small app, but means hit rate is lower with many workers. If
  you ever scale out, replace the `lru_cache` with a Redis-backed cache
  (different spec — see 08).
- Holds tensors / lists in memory. 1024 entries × 384 × 4 bytes ≈ 1.5 MB.
  Negligible.
- Rollback: delete the helper, drop the call. No schema or migration
  impact.
