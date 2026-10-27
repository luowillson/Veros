"""Glue between DB rows and the deterministic scoring formula."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, select

from app.db.models import Paper, Review, VerosScore
from app.services.dimensions import refresh_paper_browse_cache
from app.services.veros_score import ReviewSignal, ScoreResult, compute_score
from app.utils.ratings import parse_numeric


# Modern OpenReview venues (ICLR, NeurIPS, COLM…) use a 1..10 rating scale.
# Older 1..6 venues exist but cannot be reliably distinguished from a 1..10
# venue where reviewers happen to all rate ≤6. We default to 10 and let the
# caller pass an override when a venue is known to use a smaller scale.
_DEFAULT_RATING_SCALE_MAX = 10
_ICLR_2025_RATING_SCALE_MAX = 10
_ICLR_RATING_SCALE = {"rating": (1.0, 10.0)}
_ICLR_SECTION_SCALES = {
    "soundness": (1.0, 4.0),
    "presentation": (1.0, 4.0),
    "contribution": (1.0, 4.0),
}
_ICLR_SECTION_TO_STANDARD_DIMENSION = {
    "contribution": ("novelty", "impact"),
    "soundness": ("technical",),
    "presentation": ("clarity",),
}
_NEURIPS_2025_RATING_SCALE_MAX = 6
_NEURIPS_RATING_SCALE = {"rating": (1.0, 6.0)}
_NEURIPS_SECTION_SCALES = {
    "quality": (1.0, 4.0),
    "clarity": (1.0, 4.0),
    "significance": (1.0, 4.0),
    "originality": (1.0, 4.0),
}
_NEURIPS_SECTION_TO_STANDARD_DIMENSION = {
    "originality": "novelty",
    "quality": "technical",
    "clarity": "clarity",
    "significance": "impact",
}


def is_neurips_2025_paper(paper: Paper) -> bool:
    venue = (paper.venue or "").lower()
    return venue.startswith("neurips 2025")


def is_iclr_2025_paper(paper: Paper) -> bool:
    venue = (paper.venue or "").lower()
    return venue.startswith("iclr 2025")


def rating_scale_max_for_paper(paper: Paper) -> int:
    if is_iclr_2025_paper(paper):
        return _ICLR_2025_RATING_SCALE_MAX
    if is_neurips_2025_paper(paper):
        return _NEURIPS_2025_RATING_SCALE_MAX
    return _DEFAULT_RATING_SCALE_MAX


def normalize_rating_to_ten(rating: float, rating_scale_max: int) -> float:
    if rating_scale_max >= 10:
        return min(10.0, max(0.0, rating))
    if rating_scale_max <= 0:
        return rating
    rescaled = rating * (10.0 / rating_scale_max)
    return round(min(10.0, max(0.0, rescaled)) * 2) / 2


def _normalize_to_percent(value: float, minimum: float, maximum: float) -> int:
    if maximum <= minimum:
        return 0
    normalized = (value - minimum) / (maximum - minimum)
    return round(max(0.0, min(1.0, normalized)) * 100)


def _weighted_mean(values: list[tuple[float, float]]) -> float | None:
    if not values:
        return None
    weight_sum = sum(weight for _, weight in values)
    if weight_sum <= 0:
        return None
    return sum(value * weight for value, weight in values) / weight_sum


def neurips_section_breakdown(reviews: list[Review]) -> dict[str, object]:
    section_scores: dict[str, object] = {}
    standardized_dimensions: dict[str, int] = {}

    for section, (minimum, maximum) in {
        **_NEURIPS_RATING_SCALE,
        **_NEURIPS_SECTION_SCALES,
    }.items():
        values: list[tuple[float, float]] = []
        for review in reviews:
            value = parse_numeric((review.content or {}).get(section))
            if value is None:
                continue
            confidence = (
                float(review.confidence)
                if review.confidence is not None
                else parse_numeric((review.content or {}).get("confidence"))
            )
            values.append((value, confidence if confidence is not None else 3.0))

        weighted = _weighted_mean(values)
        average = (
            sum(value for value, _ in values) / len(values)
            if values
            else None
        )
        normalized = (
            _normalize_to_percent(weighted, minimum, maximum)
            if weighted is not None
            else None
        )
        section_scores[section] = {
            "confidence_weighted_score": round(weighted, 3) if weighted is not None else None,
            "average_score": round(average, 3) if average is not None else None,
            "normalized": normalized,
            "scale": {"min": minimum, "max": maximum},
            "scored_reviews": len(values),
        }

        dimension = _NEURIPS_SECTION_TO_STANDARD_DIMENSION.get(section)
        if dimension is not None and normalized is not None:
            standardized_dimensions[dimension] = normalized

    return {
        "neurips_sections": section_scores,
        "standardized_dimensions": standardized_dimensions,
    }


def iclr_section_breakdown(reviews: list[Review]) -> dict[str, object]:
    section_scores: dict[str, object] = {}
    standardized_dimensions: dict[str, int] = {}

    for section, (minimum, maximum) in {
        **_ICLR_RATING_SCALE,
        **_ICLR_SECTION_SCALES,
    }.items():
        values: list[tuple[float, float]] = []
        for review in reviews:
            value = parse_numeric((review.content or {}).get(section))
            if value is None:
                continue
            confidence = (
                float(review.confidence)
                if review.confidence is not None
                else parse_numeric((review.content or {}).get("confidence"))
            )
            values.append((value, confidence if confidence is not None else 3.0))

        weighted = _weighted_mean(values)
        average = (
            sum(value for value, _ in values) / len(values)
            if values
            else None
        )
        normalized = (
            _normalize_to_percent(weighted, minimum, maximum)
            if weighted is not None
            else None
        )
        section_scores[section] = {
            "confidence_weighted_score": round(weighted, 3) if weighted is not None else None,
            "average_score": round(average, 3) if average is not None else None,
            "normalized": normalized,
            "scale": {"min": minimum, "max": maximum},
            "scored_reviews": len(values),
        }

        dimensions = _ICLR_SECTION_TO_STANDARD_DIMENSION.get(section, ())
        if normalized is not None:
            for dimension in dimensions:
                standardized_dimensions[dimension] = normalized

    return {
        "iclr_sections": section_scores,
        "standardized_dimensions": standardized_dimensions,
    }


def compute_and_store_score(db: Session, paper_id: str) -> ScoreResult:
    """Recompute the Veros score for a paper from its current review rows."""
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise ValueError(f"paper {paper_id!r} not found")

    review_rows = db.exec(select(Review).where(Review.paper_id == paper_id)).all()

    signals = [
        ReviewSignal(
            rating=float(row.rating),
            confidence=float(row.confidence) if row.confidence is not None else 3.0,
        )
        for row in review_rows
        if row.rating is not None
    ]
    rating_scale_max = rating_scale_max_for_paper(paper)
    result = compute_score(
        signals,
        acceptance=paper.acceptance,
        rating_scale_max=rating_scale_max,
    )

    if result.status == "ok" and result.score is not None:
        breakdown = {
            **result.breakdown,
            "consensus_strength": result.consensus_strength,
            "rating_scale_max": rating_scale_max,
        }
        if is_iclr_2025_paper(paper):
            breakdown.update(iclr_section_breakdown(review_rows))
        elif is_neurips_2025_paper(paper):
            breakdown.update(neurips_section_breakdown(review_rows))

        stmt = insert(VerosScore).values(
            paper_id=paper_id,
            score=result.score,
            grade=result.grade,
            verdict=result.verdict,
            breakdown=breakdown,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[VerosScore.__table__.c.paper_id],
            set_={
                "score": stmt.excluded.score,
                "grade": stmt.excluded.grade,
                "verdict": stmt.excluded.verdict,
                "breakdown": stmt.excluded.breakdown,
                "computed_at": stmt.excluded.computed_at,
            },
        )
        db.exec(stmt)
        refresh_paper_browse_cache(db, paper_id)
        db.commit()

    return result
