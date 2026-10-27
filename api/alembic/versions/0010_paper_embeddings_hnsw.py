"""Replace the IVFFLAT vector index on paper_embeddings with HNSW.

The original index was created with `lists = 100`, sized for ~10k rows. At 400k
embeddings each probe scans ~4k vectors, which makes semantic search and the
landing-graph cluster query slower than they need to be. HNSW gives better
recall at this scale without manual `lists` / `probes` tuning.

Both index operations use CONCURRENTLY so writes to paper_embeddings stay
unblocked during the rebuild — important on the shared DB. CREATE INDEX
CONCURRENTLY cannot run inside a transaction, so the migration uses an
autocommit block.

Revision ID: 0010_paper_embeddings_hnsw
Revises: 0009_papers_trgm_lower
Create Date: 2026-05-04
"""

from alembic import op

revision = "0010_paper_embeddings_hnsw"
down_revision = "0009_papers_trgm_lower"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS paper_embeddings_hnsw
              ON paper_embeddings USING hnsw (embedding vector_cosine_ops)
              WITH (m = 16, ef_construction = 64)
            """
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS paper_embeddings_ivf")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS paper_embeddings_ivf
              ON paper_embeddings USING ivfflat (embedding vector_cosine_ops)
              WITH (lists = 100)
            """
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS paper_embeddings_hnsw")
