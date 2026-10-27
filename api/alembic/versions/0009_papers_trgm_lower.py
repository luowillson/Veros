"""Replace title/abstract trgm GIN indexes with lower(...) versions.

The query path lowercases both query and column (word_similarity(lower(q),
lower(title))), so the indexes need to be on the same lowercased expressions
to be used. The function form word_similarity(...) > k is also not index-
eligible — only the operator forms (<%, %>) are. Search code is updated in
the same change to use the operator + SET LOCAL pg_trgm.word_similarity_threshold.

Revision ID: 0009_papers_trgm_lower
Revises: 0008_paper_browse_columns
Create Date: 2026-05-04
"""

from alembic import op

revision = "0009_papers_trgm_lower"
down_revision = "0008_paper_browse_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS papers_title_trgm_lower
          ON papers USING gin (lower(title) gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS papers_abstract_trgm_lower
          ON papers USING gin (lower(COALESCE(abstract, '')) gin_trgm_ops)
        """
    )
    op.execute("DROP INDEX IF EXISTS papers_title_trgm")
    op.execute("DROP INDEX IF EXISTS papers_abstract_trgm")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS papers_title_trgm "
        "ON papers USING gin (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS papers_abstract_trgm "
        "ON papers USING gin ((COALESCE(abstract, '')) gin_trgm_ops)"
    )
    op.execute("DROP INDEX IF EXISTS papers_title_trgm_lower")
    op.execute("DROP INDEX IF EXISTS papers_abstract_trgm_lower")
