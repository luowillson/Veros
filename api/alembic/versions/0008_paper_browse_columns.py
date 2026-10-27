"""Cache score and standardized dimensions on papers for fast browse sorting.

Adds papers.score, papers.novelty, papers.technical, papers.clarity, papers.impact
and supporting indexes so the browse query can avoid the 3-way LEFT JOIN onto
veros_scores and ai_insights.

Revision ID: 0008_paper_browse_columns
Revises: 0007_paper_graph_metrics
Create Date: 2026-05-04
"""

from alembic import op

revision = "0008_paper_browse_columns"
down_revision = "0007_paper_graph_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS score NUMERIC(3,1)")
    op.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS novelty INT")
    op.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS technical INT")
    op.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS clarity INT")
    op.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS impact INT")

    # Backfill from veros_scores + ai_insights, mirroring the precedence in
    # app/services/dimensions.py::standardized_dimensions: prefer
    # breakdown.standardized_dimensions[<dim>] when present, fall back to
    # ai_insights.<dim>, otherwise leave NULL.
    op.execute(
        """
        UPDATE papers p
        SET
          score     = s.score,
          novelty   = COALESCE(
                        NULLIF(s.breakdown->'standardized_dimensions'->>'novelty', '')::int,
                        i.novelty
                      ),
          technical = COALESCE(
                        NULLIF(s.breakdown->'standardized_dimensions'->>'technical', '')::int,
                        i.technical
                      ),
          clarity   = COALESCE(
                        NULLIF(s.breakdown->'standardized_dimensions'->>'clarity', '')::int,
                        i.clarity
                      ),
          impact    = COALESCE(
                        NULLIF(s.breakdown->'standardized_dimensions'->>'impact', '')::int,
                        i.impact
                      )
        FROM veros_scores s
        LEFT JOIN ai_insights i ON i.paper_id = s.paper_id
        WHERE p.id = s.paper_id
        """
    )

    # Papers with an insight but no veros_score still want the dimension cache.
    op.execute(
        """
        UPDATE papers p
        SET
          novelty   = COALESCE(p.novelty,   i.novelty),
          technical = COALESCE(p.technical, i.technical),
          clarity   = COALESCE(p.clarity,   i.clarity),
          impact    = COALESCE(p.impact,    i.impact)
        FROM ai_insights i
        WHERE p.id = i.paper_id
          AND (p.novelty IS NULL OR p.technical IS NULL OR p.clarity IS NULL OR p.impact IS NULL)
        """
    )

    # Browse-sort indexes. Each matches the ORDER BY shape used by
    # _browse_candidate_ids for the corresponding sort key.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS papers_browse_score_idx
          ON papers (score DESC NULLS LAST, ingested_at DESC NULLS LAST, created_at DESC)
        """
    )
    for dimension in ("novelty", "technical", "clarity", "impact"):
        op.execute(
            f"""
            CREATE INDEX IF NOT EXISTS papers_browse_{dimension}_idx
              ON papers ({dimension} DESC NULLS LAST, score DESC NULLS LAST, created_at DESC)
            """
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS papers_browse_score_idx")
    for dimension in ("novelty", "technical", "clarity", "impact"):
        op.execute(f"DROP INDEX IF EXISTS papers_browse_{dimension}_idx")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS impact")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS clarity")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS technical")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS novelty")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS score")
