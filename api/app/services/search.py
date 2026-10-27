"""Full-text + semantic search over ingested papers.

Strategy: combine (1) substring ILIKE with escaped wildcards, (2) pg_trgm
``word_similarity`` on title/abstract for fuzzy matching, (3) optional
per-token fuzzy ORs for multi-word queries, and (4) pgvector cosine
nearest-neighbour on query embeddings when available. De-duplicate by
paper_id, fetch scores and AI insights, then sort either by text relevance
or by a chosen Veros dimension.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Literal, cast

from sqlalchemy import func, text as sa_text
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import AIInsight, Paper, VerosScore
from app.schemas.paper import ConsensusStrength, PaperOut, Verdict
from app.services.citations import citation_graph_status

logger = logging.getLogger(__name__)

SearchMode = Literal["auto", "topic", "specific"]

_CANDIDATE_POOL = 50  # max IDs per channel before re-ranking by score
_FUZZY_WORD_SIM_THRESHOLD = 0.16
_MIN_TOKEN_LEN = 3
_MAX_TOKENS = 5
SortKey = Literal["relevance", "score", "novelty", "technical", "clarity", "impact"]
_SORT_KEYS: set[str] = {"relevance", "score", "novelty", "technical", "clarity", "impact"}
_DIMENSION_SORT_SQL = {
    "novelty": "novelty",
    "technical": "technical",
    "clarity": "clarity",
    "impact": "impact",
}


def _escape_ilike(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _set_word_similarity_threshold(db: Session, threshold: float) -> None:
    """SET LOCAL the threshold used by the `<%` operator for this transaction.

    The GIN trgm index can only short-circuit `:q <% col` candidates that meet
    pg_trgm.word_similarity_threshold (default 0.6). Without this, our 0.16
    threshold would be applied as a post-filter — too late to avoid the scan.
    """
    # SET is a utility statement and does not accept bind parameters; use the
    # set_config() function form, which does.
    db.execute(
        sa_text("SELECT set_config('pg_trgm.word_similarity_threshold', :t, true)"),
        {"t": str(float(threshold))},
    )


def _fuzzy_text_candidate_ids(db: Session, q: str) -> list[str]:
    """ILIKE substring + word_similarity; extra per-token rows for multi-word queries.

    Uses the `<%` operator and lowercased ILIKE against the
    papers_title_trgm_lower / papers_abstract_trgm_lower GIN indexes so the
    candidate WHERE collapses to an index scan instead of seq-scanning 400k rows.
    The ILIKE OR-arms are load-bearing: without them the planner discards the
    trgm index for `<%` alone (selectivity estimate too loose at 0.16) and
    falls back to a parallel seq scan, which is ~10x slower.
    """
    qn = q.strip()
    if not qn:
        return []

    qn_lower = qn.lower()
    like = f"%{_escape_ilike(qn_lower)}%"
    out: list[str] = []
    seen: set[str] = set()

    _set_word_similarity_threshold(db, _FUZZY_WORD_SIM_THRESHOLD)

    sql = sa_text(
        """
        SELECT id FROM papers
        WHERE
          lower(title) ILIKE :like ESCAPE '\\'
          OR lower(COALESCE(abstract, '')) ILIKE :like ESCAPE '\\'
          OR :qn_lower <% lower(title)
          OR :qn_lower <% lower(COALESCE(abstract, ''))
        ORDER BY
          (
            CASE
              WHEN lower(title) ILIKE :like ESCAPE '\\'
                OR lower(COALESCE(abstract, '')) ILIKE :like ESCAPE '\\'
              THEN 1
              ELSE 0
            END
          ) DESC,
          GREATEST(
            word_similarity(:qn_lower, lower(title)),
            word_similarity(:qn_lower, lower(COALESCE(abstract, '')))
          ) DESC
        LIMIT :lim
        """
    )
    rows = db.execute(
        sql,
        {
            "like": like,
            "qn_lower": qn_lower,
            "lim": _CANDIDATE_POOL,
        },
    ).fetchall()
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            out.append(r[0])

    tokens = [
        t.lower()
        for t in re.split(r"\s+", qn)
        if len(t) >= _MIN_TOKEN_LEN
    ][: _MAX_TOKENS]
    if len(tokens) > 1:
        tsql = sa_text(
            """
            SELECT id FROM papers
            WHERE
              :tok <% lower(title)
              OR :tok <% lower(COALESCE(abstract, ''))
            ORDER BY GREATEST(
              word_similarity(:tok, lower(title)),
              word_similarity(:tok, lower(COALESCE(abstract, '')))
            ) DESC
            LIMIT :lim
            """
        )
        for tok in tokens:
            if len(out) >= _CANDIDATE_POOL:
                break
            rows2 = db.execute(
                tsql,
                {
                    "tok": tok,
                    "lim": min(20, _CANDIDATE_POOL),
                },
            ).fetchall()
            for r in rows2:
                if r[0] not in seen:
                    seen.add(r[0])
                    out.append(r[0])
                    if len(out) >= _CANDIDATE_POOL:
                        break

    return out


def top_title_similarity(db: Session, q: str) -> float:
    """Return the max word_similarity(lower(q), lower(title)) across papers.

    The `<%` filter narrows to GIN-trgm-index candidates before the MAX runs,
    so the 400k-row seq scan is avoided. If nothing meets the threshold the
    similarity is effectively below it; report 0.0.
    """
    qn = q.strip()
    if not qn:
        return 0.0
    qn_lower = qn.lower()
    _set_word_similarity_threshold(db, _FUZZY_WORD_SIM_THRESHOLD)
    sql = sa_text(
        """
        SELECT COALESCE(MAX(word_similarity(:qn_lower, lower(title))), 0.0)
        FROM papers
        WHERE :qn_lower <% lower(title)
        """
    )
    row = db.execute(sql, {"qn_lower": qn_lower}).first()
    return float(row[0]) if row and row[0] is not None else 0.0


def looks_like_title(q: str) -> bool:
    """Heuristic: does this query look like a paper title (vs. a topic keyword)?

    Independent of DB content so titles for papers we haven't ingested still
    classify as specific and trigger an OpenReview lookup.
    """
    qn = q.strip()
    if not qn:
        return False
    words = [w for w in re.split(r"\s+", qn) if w]
    n = len(words)
    if n <= 2:
        return False
    # 5+ words: very likely a paper title.
    if n >= 5:
        return True
    # 3–4 words: require title-case-ish capitalization on most words.
    capitalized = sum(1 for w in words if w[:1].isupper())
    return capitalized >= n - 1


def classify_intent(db: Session, q: str) -> dict[str, object]:
    """Return {'mode': 'topic'|'specific', 'top_sim': float} for a submitted query.

    A query is "specific" if it's already close to a known paper title OR if it
    looks like a title on its face (so we still hit OpenReview when our DB is
    cold).
    """
    qn = q.strip()
    if not qn:
        return {"mode": "topic", "top_sim": 0.0}
    top = top_title_similarity(db, qn)
    threshold = get_settings().search_specific_paper_threshold
    if top >= threshold or looks_like_title(qn):
        return {"mode": "specific", "top_sim": top}
    return {"mode": "topic", "top_sim": top}


def best_title_match_id(db: Session, q: str) -> tuple[str | None, float]:
    """Return (paper_id, score) of the best title-similarity match, if any.

    Filters to GIN-trgm-index candidates via `<%` before sorting; falls back
    cleanly to (None, 0.0) when nothing matches above the threshold.
    """
    qn = q.strip()
    if not qn:
        return None, 0.0
    qn_lower = qn.lower()
    _set_word_similarity_threshold(db, _FUZZY_WORD_SIM_THRESHOLD)
    sql = sa_text(
        """
        SELECT id, word_similarity(:qn_lower, lower(title)) AS sim
        FROM papers
        WHERE :qn_lower <% lower(title)
        ORDER BY sim DESC
        LIMIT 1
        """
    )
    row = db.execute(sql, {"qn_lower": qn_lower}).first()
    if row is None:
        return None, 0.0
    return str(row[0]), float(row[1] or 0.0)


def _has_embeddings(db: Session) -> bool:
    row = db.execute(sa_text("SELECT EXISTS (SELECT 1 FROM paper_embeddings LIMIT 1)")).first()
    return bool(row and row[0])


@lru_cache(maxsize=1024)
def _cached_query_embedding(model_name: str, q_norm: str) -> tuple[float, ...]:
    """Cache SBERT encodes of normalized query strings; keyed on (model, query)
    so a model swap invalidates implicitly. The provider singleton stays loaded
    across calls so cache hits skip the model entirely."""
    from app.services.embeddings.factory import get_embedding_provider

    provider = get_embedding_provider()
    return tuple(provider.encode([q_norm])[0])


def _semantic_candidate_ids(db: Session, q: str) -> list[str]:
    qn = " ".join(q.split()).lower()
    if not qn or not _has_embeddings(db):
        return []

    model_name = get_settings().embedding_model
    embedding = _cached_query_embedding(model_name, qn)
    vec_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"
    sql = sa_text(
        "SELECT paper_id FROM paper_embeddings "
        "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :n"
    )
    rows = db.execute(sql, {"vec": vec_str, "n": _CANDIDATE_POOL}).fetchall()
    return [r[0] for r in rows]


def _dedupe_ids(candidate_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_ids: list[str] = []
    for pid in candidate_ids:
        if pid not in seen:
            seen.add(pid)
            unique_ids.append(pid)
    return unique_ids


def _query_candidate_ids(db: Session, q: str) -> list[str]:
    candidate_ids = list(_fuzzy_text_candidate_ids(db, q))
    try:
        candidate_ids.extend(_semantic_candidate_ids(db, q))
    except Exception:
        logger.debug("Vector search skipped (provider unavailable or no embeddings).")
    return _dedupe_ids(candidate_ids)


def _browse_candidate_ids(
    db: Session,
    limit: int,
    offset: int,
    sort_by: SortKey = "score",
) -> list[str]:
    """Return browse-page IDs ranked globally by score or a standardized dimension.

    Sorts on the cached papers.score / papers.<dimension> columns so the query
    hits one of the papers_browse_*_idx indexes — no joins, single index scan.
    """
    if sort_by == "relevance":
        sort_by = "score"

    if sort_by == "score":
        order = "score DESC NULLS LAST, ingested_at DESC NULLS LAST, created_at DESC"
    else:
        column = _DIMENSION_SORT_SQL[sort_by]
        order = f"{column} DESC NULLS LAST, score DESC NULLS LAST, created_at DESC"

    sql = sa_text(
        f"""
        SELECT id FROM papers
        ORDER BY {order}
        LIMIT :lim OFFSET :off
        """
    )
    rows = db.execute(sql, {"lim": limit, "off": offset}).fetchall()
    return [r[0] for r in rows]


def build_paper_out(
    paper: Paper,
    score_row: VerosScore | None,
    insight: AIInsight | None,
) -> PaperOut:
    breakdown = dict(score_row.breakdown) if score_row else {}
    cs_raw = breakdown.get("consensus_strength", "split")
    cs: ConsensusStrength = (
        cast(ConsensusStrength, cs_raw)
        if cs_raw in {"strong", "moderate", "mixed", "split"}
        else "split"
    )
    reviewer_count = int(breakdown.get("n_reviews", 0))

    return PaperOut(
        id=paper.id,
        title=paper.title,
        authors=", ".join(paper.authors) if paper.authors else "Unknown",
        venue=paper.venue,
        citations=paper.citations,
        references_count=paper.references_count,
        citation_graph_status=citation_graph_status(paper),
        openreview_url=paper.openreview_url,
        acceptance=paper.acceptance,
        score=float(paper.score) if paper.score is not None else None,
        grade=score_row.grade if score_row else "—",
        verdict=cast(Verdict, score_row.verdict) if score_row else "Insufficient reviews",
        novelty=paper.novelty,
        technical=paper.technical,
        clarity=paper.clarity,
        impact=paper.impact,
        tldr=insight.tldr if insight else None,
        consensus=insight.consensus if insight else None,
        consensus_strength=cs,
        reviewer_count=reviewer_count,
    )


def _relevance_score(db: Session, q: str, paper_ids: list[str]) -> dict[str, float]:
    """Return per-paper title/abstract relevance for the given query."""
    if not paper_ids or not q.strip():
        return {}
    sql = sa_text(
        """
        SELECT id,
               GREATEST(
                 word_similarity(lower(:qn), lower(title)),
                 0.5 * word_similarity(lower(:qn), lower(COALESCE(abstract, '')))
               ) AS rel,
               (CASE WHEN title ILIKE :like ESCAPE '\\' THEN 1 ELSE 0 END) AS title_hit
        FROM papers
        WHERE id = ANY(:ids)
        """
    )
    like = f"%{_escape_ilike(q.strip())}%"
    rows = db.execute(
        sql, {"qn": q.strip(), "like": like, "ids": paper_ids}
    ).fetchall()
    return {str(r[0]): float(r[1] or 0.0) + (0.25 if r[2] else 0.0) for r in rows}


def build_results_for_ids(db: Session, unique_ids: list[str]) -> list[PaperOut]:
    if not unique_ids:
        return []

    papers = {
        p.id: p
        for p in db.exec(select(Paper).where(Paper.id.in_(unique_ids))).all()
    }
    scores = {
        s.paper_id: s
        for s in db.exec(
            select(VerosScore).where(VerosScore.paper_id.in_(unique_ids))
        ).all()
    }
    insights = {
        i.paper_id: i
        for i in db.exec(
            select(AIInsight).where(AIInsight.paper_id.in_(unique_ids))
        ).all()
    }

    return [
        build_paper_out(papers[pid], scores.get(pid), insights.get(pid))
        for pid in unique_ids
        if pid in papers
    ]

def _sort_results(
    db: Session,
    q: str,
    results: list[PaperOut],
    mode: SearchMode,
    sort_by: SortKey,
) -> None:
    if q and sort_by == "relevance":
        rel = _relevance_score(db, q, [r.id for r in results])
        results.sort(key=lambda x: rel.get(x.id, 0.0), reverse=True)
        return

    # PaperOut has no `relevance` attribute — for the empty-query browse path,
    # "relevance" collapses to score (matching _browse_candidate_ids).
    effective = "score" if sort_by == "relevance" else sort_by
    results.sort(
        key=lambda x: (
            getattr(x, effective) if getattr(x, effective) is not None else 0.0,
            x.score or 0.0,
        ),
        reverse=True,
    )


def count_papers(db: Session, query: str) -> int:
    """Return total result count for the given query (used for pagination).

    For empty (browse) queries, use Postgres' planner estimate from pg_class
    instead of a real COUNT(*). At 400k+ rows the exact count is hundreds of
    ms uncached, and the UI only needs an order-of-magnitude figure.
    """
    q = query.strip()
    if not q:
        row = db.execute(
            sa_text("SELECT reltuples::bigint FROM pg_class WHERE relname = 'papers'")
        ).first()
        if row and row[0] is not None and int(row[0]) > 0:
            return int(row[0])
        return db.exec(select(func.count(Paper.id))).one()  # type: ignore[return-value]

    return len(_query_candidate_ids(db, q))


def search_papers(
    db: Session,
    query: str,
    limit: int = 20,
    offset: int = 0,
    mode: SearchMode = "auto",
    sort_by: SortKey = "relevance",
) -> list[PaperOut]:
    q = query.strip()
    if sort_by not in _SORT_KEYS:
        sort_by = "relevance"

    if not q:
        results = build_results_for_ids(
            db, _browse_candidate_ids(db, limit, offset, sort_by)
        )
        _sort_results(db, q, results, mode, sort_by)
        return results

    results = build_results_for_ids(db, _query_candidate_ids(db, q))
    _sort_results(db, q, results, mode, sort_by)
    return results[offset : offset + limit]


def search_papers_with_total(
    db: Session,
    query: str,
    limit: int = 20,
    offset: int = 0,
    mode: SearchMode = "auto",
    sort_by: SortKey = "relevance",
) -> tuple[list[PaperOut], int]:
    q = query.strip()
    if sort_by not in _SORT_KEYS:
        sort_by = "relevance"

    if not q:
        total = count_papers(db, q)
        return search_papers(
            db, q, limit=limit, offset=offset, mode=mode, sort_by=sort_by
        ), total

    results = build_results_for_ids(db, _query_candidate_ids(db, q))
    _sort_results(db, q, results, mode, sort_by)
    return results[offset : offset + limit], len(results)
