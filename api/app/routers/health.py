from fastapi import APIRouter, Response
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.db.models import Paper, Review
from app.deps import DbSession
from app.schemas.graph import LandingGraph
from app.services.landing_graph import build_landing_graph

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: DbSession) -> dict[str, str]:
    db.exec(text("SELECT 1"))
    return {"status": "ok"}


def _approx_row_count(db: Session, table: str) -> int:
    row = db.execute(
        text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"),
        {"t": table},
    ).first()
    return int(row[0]) if row and row[0] is not None and int(row[0]) > 0 else -1


@router.get("/stats")
def stats(response: Response, db: DbSession) -> dict[str, int]:
    """Counts of ingested papers and reviews for the landing page."""
    paper_count = _approx_row_count(db, "papers")
    if paper_count < 0:
        paper_count = db.exec(select(func.count()).select_from(Paper)).one()  # type: ignore[arg-type]
    review_count = _approx_row_count(db, "reviews")
    if review_count < 0:
        review_count = db.exec(select(func.count()).select_from(Review)).one()  # type: ignore[arg-type]
    response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=600"
    return {"paper_count": paper_count, "review_count": review_count}


@router.get("/landing/graph", response_model=LandingGraph)
def landing_graph(db: DbSession) -> LandingGraph:
    """Small semantic graph slice used on the marketing landing page."""
    return build_landing_graph(db)
