from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, Column, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Paper(SQLModel, table=True):
    __tablename__ = "papers"

    id: str = Field(primary_key=True)
    title: str
    authors: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(String), nullable=False))
    venue: str | None = None
    year: int | None = None
    citations: int | None = None
    references_count: int | None = None
    abstract: str | None = None
    openreview_url: str | None = None
    acceptance: str | None = None
    citation_metadata: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    citation_enriched_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    ingested_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    analyzed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    # Cached browse-sort columns. Mirrored from veros_scores.score and
    # standardized_dimensions(score_row, insight) so /search/page can sort
    # without joining veros_scores + ai_insights.
    score: float | None = Field(default=None, sa_column=Column(Numeric(3, 1)))
    novelty: int | None = None
    technical: int | None = None
    clarity: int | None = None
    impact: int | None = None


class PaperIdentifier(SQLModel, table=True):
    __tablename__ = "paper_identifiers"
    __table_args__ = (
        Index("paper_identifiers_lookup_idx", "namespace", "value", unique=True),
    )

    paper_id: str = Field(
        sa_column=Column(
            String, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    namespace: str = Field(primary_key=True)
    value: str = Field(primary_key=True)
    confidence: float = Field(default=1.0, sa_column=Column(Numeric(4, 3), nullable=False))
    source: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Review(SQLModel, table=True):
    __tablename__ = "reviews"
    __table_args__ = (Index("reviews_paper_id_idx", "paper_id"),)

    id: str = Field(primary_key=True)
    paper_id: str = Field(sa_column=Column(String, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False))
    invitation: str | None = None
    signatures: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(String), nullable=False))
    rating: float | None = Field(default=None, sa_column=Column(Numeric(3, 1)))
    confidence: float | None = Field(default=None, sa_column=Column(Numeric(3, 1)))
    recommendation: str | None = None
    content: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    created_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))


class AIInsight(SQLModel, table=True):
    __tablename__ = "ai_insights"

    paper_id: str = Field(
        sa_column=Column(
            String, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    tldr: str
    deep: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(String), nullable=False))
    skim: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(String), nullable=False))
    reviewer_voices: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    novelty: int
    technical: int
    clarity: int
    impact: int
    consensus: str
    model: str
    prompt_version: int = 1
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class VerosScore(SQLModel, table=True):
    __tablename__ = "veros_scores"

    paper_id: str = Field(
        sa_column=Column(
            String, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    score: float = Field(sa_column=Column(Numeric(3, 1), nullable=False))
    grade: str
    verdict: str
    breakdown: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    computed_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class SavedPaper(SQLModel, table=True):
    __tablename__ = "saved_papers"

    user_id: str = Field(primary_key=True)
    paper_id: str = Field(
        sa_column=Column(
            String, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    saved_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PaperEmbedding(SQLModel, table=True):
    __tablename__ = "paper_embeddings"

    paper_id: str = Field(
        sa_column=Column(
            String, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    embedding: list[float] = Field(sa_column=Column(Vector(384), nullable=False))
    source: str
    model: str


class PaperConcept(SQLModel, table=True):
    __tablename__ = "paper_concepts"

    paper_id: str = Field(
        sa_column=Column(
            String, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    concept: str = Field(primary_key=True)
    weight: float = Field(sa_column=Column(Numeric(4, 3), nullable=False))
    source: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PaperEdge(SQLModel, table=True):
    __tablename__ = "paper_edges"

    src_paper_id: str = Field(
        sa_column=Column(
            String, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    dst_paper_id: str = Field(
        sa_column=Column(
            String, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    edge_type: str = Field(primary_key=True)
    weight: float = Field(sa_column=Column(Numeric(4, 3), nullable=False))
    edge_metadata: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class LearningPathway(SQLModel, table=True):
    __tablename__ = "learning_pathways"

    id: str = Field(primary_key=True)
    user_id: str | None = None
    seed_paper_id: str | None = Field(
        default=None,
        sa_column=Column(String, ForeignKey("papers.id", ondelete="SET NULL"), nullable=True),
    )
    query_text: str | None = None
    title: str
    rationale: str
    status: str = Field(default="ready")
    model: str | None = None
    enrichment_notes: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class LearningPathwayItem(SQLModel, table=True):
    __tablename__ = "learning_pathway_items"

    pathway_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("learning_pathways.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    position: int = Field(primary_key=True)
    paper_id: str | None = Field(
        default=None,
        sa_column=Column(
            String, ForeignKey("papers.id", ondelete="CASCADE"), nullable=True
        ),
    )
    stage: str
    why_this_paper: str
    read_focus: str
    match_quality: str = Field(default="strong")
    search_query: str | None = None
    anchor_concepts: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    score: float | None = Field(default=None, sa_column=Column(Numeric(4, 3)))


class OpenReviewIngestFailure(SQLModel, table=True):
    __tablename__ = "openreview_ingest_failures"

    paper_id: str = Field(primary_key=True)
    attempts: int
    error: str = Field(sa_column=Column(Text, nullable=False))
    failed_at: datetime = Field(
        default_factory=lambda: datetime.now(),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
