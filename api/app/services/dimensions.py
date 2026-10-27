from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.db.models import AIInsight, Paper, VerosScore


def _dimension_from_breakdown(
    breakdown: dict[str, Any],
    name: str,
) -> int | None:
    dimensions = breakdown.get("standardized_dimensions")
    if not isinstance(dimensions, dict):
        return None
    value = dimensions.get(name)
    if isinstance(value, (int, float)):
        return round(max(0.0, min(100.0, float(value))))
    return None


def standardized_dimensions(
    score_row: VerosScore | None,
    insight: AIInsight | None,
) -> dict[str, int | None]:
    breakdown = dict(score_row.breakdown or {}) if score_row else {}
    novelty = _dimension_from_breakdown(breakdown, "novelty")
    technical = _dimension_from_breakdown(breakdown, "technical")
    clarity = _dimension_from_breakdown(breakdown, "clarity")
    impact = _dimension_from_breakdown(breakdown, "impact")
    return {
        "novelty": novelty if novelty is not None else (insight.novelty if insight else None),
        "technical": technical if technical is not None else (insight.technical if insight else None),
        "clarity": clarity if clarity is not None else (insight.clarity if insight else None),
        "impact": impact if impact is not None else (insight.impact if insight else None),
    }


def refresh_paper_browse_cache(db: Session, paper_id: str) -> None:
    """Refresh papers.score + dimension columns from veros_scores and ai_insights.

    Called from scoring.py and analyze.py after they upsert their respective
    rows so the browse-sort indexes stay in sync. Does not commit.
    """
    paper = db.get(Paper, paper_id)
    if paper is None:
        return
    score_row = db.get(VerosScore, paper_id)
    insight = db.get(AIInsight, paper_id)
    dims = standardized_dimensions(score_row, insight)
    paper.score = float(score_row.score) if score_row is not None else None
    paper.novelty = dims["novelty"]
    paper.technical = dims["technical"]
    paper.clarity = dims["clarity"]
    paper.impact = dims["impact"]
    db.add(paper)
