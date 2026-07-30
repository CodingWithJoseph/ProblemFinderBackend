
import uuid
from app.db import Base
from datetime import datetime, date, timezone
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID, ARRAY
from sqlalchemy import String, Text, Integer, BigInteger, Boolean, Float, Date, TIMESTAMP, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
# Embedding width produced by the AI pipeline (text-embedding-3-large). Both the
# per-item embedding and the cluster centroid use this dimensionality. Mapped as
# deferred so list/detail queries never pull the vector over the wire.
EMBEDDING_DIM = 3072


# ============================================================
# Pipeline-produced tables (simplified 6-table Supabase schema)
#
# The AI pipeline (ProblemFinderAI) writes these; the API only reads them. The
# product tables include pipeline_runs, clusters, cluster_items,
# cluster_neighbors and cluster_signals. The API models the read surfaces it
# needs for discovery and the Signal workspace.
# ============================================================


class Cluster(Base):
    __tablename__ = "clusters"

    # Pipeline-written columns (see ProblemFinderAI transform_cluster: the
    # publish step writes id, name, summary, signal_score, pipeline_version).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Deterministic 0.0-1.0 signal score (volume + recency). Primary ranking key
    # for the dashboard, trending and discovery feeds.
    signal_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Lightweight cluster-level analytics the API surfaces directly.
    trending: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    first_seen: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seen_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    author_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    community_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_breakdown: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    post_volume_by_date: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # pgvector centroid used by the pipeline for assignment; deferred so it is
    # never loaded unless explicitly requested.
    centroid: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True, deferred=True)


class ClusterSignal(Base):
    """Read model for the founder-facing ``cluster_signals`` payload.

    Mirrors the live Supabase schema column-for-column (verified via
    ``information_schema``): all 21 columns are mapped, including
    ``input_fingerprint`` / ``last_error`` and the ``created_at`` / ``updated_at``
    bookkeeping timestamps (NOT NULL, ``now()`` defaults). ``status`` is a
    ``cluster_signal_status`` enum in Postgres; mapping it as text is read-safe.
    """

    __tablename__ = "cluster_signals"

    cluster_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    input_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recency: Mapped[float | None] = mapped_column(Float, nullable=True)
    momentum_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    momentum_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    momentum_90d: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_volume_by_week: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    total_posts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    community_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_communities: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    avg_comments: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_votes: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_problem_statements: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class ClusterItem(Base):
    """One conveyor row per external post â€” the flat table that replaced the
    old ``source_posts`` â‹ˆ ``cluster_members`` â‹ˆ ``opportunities`` triad.

    ``cluster_id`` is NULL until the pipeline assigns the item to a cluster. The
    LLM-derived ``problem_statement`` / ``solution_angle`` fields support
    Signal and problem-breakdown views.
    """

    __tablename__ = "cluster_items"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Source post fields.
    platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    community: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_item_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    permalink: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    scraped_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Classifier output.
    opportunity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    opportunity_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    problem_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    solution_angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Assignment metadata.
    distance_to_centroid: Mapped[float | None] = mapped_column(Float, nullable=True)
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    assigned_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True, deferred=True)


class ClusterSnapshot(Base):
    __tablename__ = "cluster_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    post_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_posts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    avg_comments: Mapped[float | None] = mapped_column(Float, nullable=True)


# ============================================================
# Released user/product tables owned by the API
# ============================================================


class SearchSession(Base):
    __tablename__ = "search_sessions"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False, default="New search")
    saved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)
    last_activity_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SearchTurn(Base):
    __tablename__ = "search_turns"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("search_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    interpretation: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("search_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    draft: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result_cluster_ids: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=list)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Pipeline(Base):
    __tablename__ = "pipeline"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    project_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_ids: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=list)
    source_cluster_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage: Mapped[str] = mapped_column(String(50), nullable=False, default="watching")
    launched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class SignalAnalysisCase(Base):
    __tablename__ = "signal_analysis_cases"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("pipeline.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress_step: Mapped[str | None] = mapped_column(String(48), nullable=True)
    progress_label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    safe_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    current_version_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("signal_analysis_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SignalAnalysisVersion(Base):
    __tablename__ = "signal_analysis_versions"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("signal_analysis_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(48), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    analysis: Mapped[dict] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SignalCaseOverride(Base):
    __tablename__ = "signal_case_overrides"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("signal_analysis_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    object_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    object_id: Mapped[str] = mapped_column(String(160), nullable=False)
    patch: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SignalAnalysisJob(Base):
    __tablename__ = "signal_analysis_jobs"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("signal_analysis_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    source_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SignalConversation(Base):
    __tablename__ = "signal_conversations"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("signal_analysis_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False, default="Signal conversation")
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SignalConversationTurn(Base):
    __tablename__ = "signal_conversation_turns"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("signal_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    proposal: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    insufficient_evidence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )



class Problem(Base):
    __tablename__ = "problems"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_post_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list | None] = mapped_column(Vector(1536), nullable=True)
    pain_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    urgency_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    frequency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_alternatives: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
