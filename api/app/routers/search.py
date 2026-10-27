from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from app.deps import DbSession
from app.schemas.paper import PaperOut
from app.services.ingest_failures import get_ingest_failure
from app.services.openreview_search import find_best_openreview_match
from app.services.search import (
    SearchMode,
    SortKey,
    best_title_match_id,
    classify_intent,
    search_papers,
    search_papers_with_total,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


class SearchPageResponse(BaseModel):
    results: list[PaperOut]
    total: int


@router.get("/page", response_model=SearchPageResponse)
def search_page(
    response: Response,
    db: DbSession,
    q: str = Query(default="", description="Search query — title keywords or semantic text"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort: SortKey = Query(
        default="relevance",
        description="Sort results by relevance, score, novelty, technical, clarity, or impact",
    ),
    mode: SearchMode = Query(
        default="auto",
        description="auto | topic | specific (relevance is the default sort for non-empty queries)",
    ),
) -> SearchPageResponse:
    """Search ingested papers and return the page plus total using one candidate pass."""
    results, total = search_papers_with_total(
        db, q, limit=limit, offset=offset, mode=mode, sort_by=sort
    )
    if not q.strip():
        # Browse pagination is identical for everyone; let CDN/browser cache it.
        response.headers["Cache-Control"] = (
            "public, max-age=30, stale-while-revalidate=300"
        )
    return SearchPageResponse(results=results, total=total)


@router.get("", response_model=list[PaperOut])
def search(
    db: DbSession,
    q: str = Query(default="", description="Search query — title keywords or semantic text"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort: SortKey = Query(
        default="relevance",
        description="Sort results by relevance, score, novelty, technical, clarity, or impact",
    ),
    mode: SearchMode = Query(
        default="auto",
        description="auto | topic | specific (relevance is the default sort for non-empty queries)",
    ),
) -> list[PaperOut]:
    """Search ingested papers by title/abstract text and semantic similarity."""
    return search_papers(db, q, limit=limit, offset=offset, mode=mode, sort_by=sort)


class LookupRequest(BaseModel):
    q: str


class LookupCandidate(BaseModel):
    id: str
    title: str
    venue: str | None = None


class LookupResponse(BaseModel):
    intent: Literal["topic", "specific"]
    top_sim: float
    paper_id: str | None
    ingest_started: bool
    openreview_found: bool
    openreview_candidate: LookupCandidate | None
    results: list[PaperOut]


# In-DB confidence above which we treat the best title match as definitively the
# paper the user wants — skip the OpenReview round-trip entirely.
_IN_DB_CONFIDENT_MATCH = 0.85


@router.post("/lookup", response_model=LookupResponse)
def lookup(
    req: LookupRequest,
    db: DbSession,
    include_results: bool = Query(default=True),
) -> LookupResponse:
    """Classify the query, optionally pull a missing paper from OpenReview, return results."""
    q = req.q.strip()
    if not q:
        return LookupResponse(
            intent="topic",
            top_sim=0.0,
            paper_id=None,
            ingest_started=False,
            openreview_found=False,
            openreview_candidate=None,
            results=search_papers(db, "", mode="topic") if include_results else [],
        )

    intent_info = classify_intent(db, q)
    intent = intent_info["mode"]
    top_sim = float(intent_info["top_sim"])  # type: ignore[arg-type]

    if intent == "topic":
        return LookupResponse(
            intent="topic",
            top_sim=top_sim,
            paper_id=None,
            ingest_started=False,
            openreview_found=False,
            openreview_candidate=None,
            results=search_papers(db, q, mode="topic") if include_results else [],
        )

    # Specific intent. If the in-DB match is very confident, surface it directly.
    if top_sim >= _IN_DB_CONFIDENT_MATCH:
        match_id, _ = best_title_match_id(db, q)
        return LookupResponse(
            intent="specific",
            top_sim=top_sim,
            paper_id=match_id,
            ingest_started=False,
            openreview_found=False,
            openreview_candidate=None,
            results=search_papers(db, q, mode="specific") if include_results else [],
        )

    # Otherwise try OpenReview before giving up.
    or_match = None
    try:
        or_match = find_best_openreview_match(q)
    except Exception:
        logger.exception("OpenReview lookup raised for q=%r", q)

    if or_match is None:
        return LookupResponse(
            intent="specific",
            top_sim=top_sim,
            paper_id=None,
            ingest_started=False,
            openreview_found=False,
            openreview_candidate=None,
            results=search_papers(db, q, mode="specific") if include_results else [],
        )

    if get_ingest_failure(db, or_match.candidate.id) is not None:
        return LookupResponse(
            intent="specific",
            top_sim=top_sim,
            paper_id=None,
            ingest_started=False,
            openreview_found=False,
            openreview_candidate=None,
            results=search_papers(db, q, mode="specific") if include_results else [],
        )

    # Kick off async ingest; the new paper will appear in /papers/{id}/status.
    from app.workers.tasks import ingest_paper_task  # noqa: PLC0415  late import

    try:
        ingest_paper_task.delay(or_match.candidate.id)
        ingest_started = True
    except Exception:
        logger.exception(
            "Failed to enqueue ingest for OpenReview paper %s", or_match.candidate.id
        )
        ingest_started = False

    return LookupResponse(
        intent="specific",
        top_sim=top_sim,
        paper_id=or_match.candidate.id,
        ingest_started=ingest_started,
        openreview_found=True,
        openreview_candidate=LookupCandidate(
            id=or_match.candidate.id,
            title=or_match.candidate.title,
            venue=or_match.candidate.venue,
        ),
        results=search_papers(db, q, mode="specific") if include_results else [],
    )
