import enum
import uuid
from app.db import Base
from datetime import datetime, date, timezone
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID, ARRAY
from sqlalchemy import String, Text, Integer, BigInteger, Boolean, Float, DateTime, Date, TIMESTAMP, ForeignKey
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.services.monitoring.schema import (
    MONITOR_ALERT_SETTINGS,
    MONITOR_ALERTS,
    MONITOR_ERROR_EVENTS,
    MONITOR_ERROR_GROUPS,
    MONITOR_INVESTIGATION_ENTRIES,
    MONITOR_INVESTIGATIONS,
    MONITOR_LOGS,
    MONITOR_PROBLEMS,
    MONITOR_REPORTS,
    MONITOR_SPANS,
    MONITOR_USAGE_EVENTS,
    MONITOR_USAGE_SOURCES,
    MONITOR_WEB_VITALS,
)

# Embedding width produced by the AI pipeline (text-embedding-3-large). Both the
# per-item embedding and the cluster centroid use this dimensionality. Mapped as
# deferred so list/detail queries never pull the vector over the wire.
EMBEDDING_DIM = 3072


class IntensityEnum(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"


class StageEnum(str, enum.Enum):
    watching = "watching"
    exploring = "exploring"
    validating = "validating"
    building = "building"


class ProductStatusEnum(str, enum.Enum):
    active = "active"
    paused = "paused"
    killed = "killed"


class PlanEnum(str, enum.Enum):
    free = "free"
    pro = "pro"
    elite = "elite"
    admin = "admin"


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
    """One conveyor row per external post — the flat table that replaced the
    old ``source_posts`` ⋈ ``cluster_members`` ⋈ ``opportunities`` triad.

    ``cluster_id`` is NULL until the pipeline assigns the item to a cluster. The
    LLM-derived ``problem_statement`` / ``solution_angle`` are the per-item
    signal the MVP Signals / Problem-breakdown / Task-list views read.
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
# User / product tables (owned by the API, not the pipeline)
# ============================================================


class UserFlag(Base):
    __tablename__ = "user_flags"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # References a cluster_items.id (the unit users flag in the simplified schema).
    opportunity_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), nullable=False)
    pipeline_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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


class SearchUsage(Base):
    __tablename__ = "search_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    query: Mapped[str] = mapped_column(String(500), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    searched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now(timezone.utc))


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


class Subscription(Base):
    __tablename__ = "subscriptions"

    uid: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan: Mapped[PlanEnum] = mapped_column(SAEnum(PlanEnum), default=PlanEnum.free)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)


class Pipeline(Base):
    __tablename__ = "pipeline"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    team_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # User-given project name; falls back to ``name`` (the cluster name) when null.
    project_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_ids: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=list)
    source_cluster_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage: Mapped[str] = mapped_column(String(50), nullable=False, default="watching")
    kill_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    distribution_channels: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=list)
    concept_angles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    exit_checklist: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revenue_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    cluster_metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    mrr: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_noted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # Optional launch timeline (Phase 1). ``timeline_start`` is set to "now" when
    # the timeline is chosen; ``timeline_target_launch`` = start + timeline_days.
    timeline_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeline_start: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    timeline_target_launch: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    launched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class PipelineStageEvent(Base):
    """Append-only log of a project entering a pipeline stage, so the Timeline can
    decompose the pre-launch journey into real stage durations (watching ->
    exploring -> validating -> building) instead of a fixed approximation."""

    __tablename__ = "pipeline_stage_events"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    entered_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


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


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class TeamMember(Base):
    __tablename__ = "team_members"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    team_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # Email-invite acceptance: the token is the secret embedded in the invite
    # link; it is single-use and cleared once the invite is accepted.
    invite_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    invite_token_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    team_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True)
    assignee_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey("team_members.id", ondelete="SET NULL"), nullable=True, index=True)
    pipeline_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="SET NULL"), nullable=True, index=True)
    parent_issue_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey("issues.id", ondelete="CASCADE"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    issue_type: Mapped[str] = mapped_column(String(50), nullable=False, default="issue")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class IssueComment(Base):
    __tablename__ = "issue_comments"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    issue_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorUsageSource(Base):
    __tablename__ = MONITOR_USAGE_SOURCES.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    public_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="Website usage snippet")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected")
    product_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    allowed_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    last_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class MonitorUsageEvent(Base):
    __tablename__ = MONITOR_USAGE_EVENTS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey(MONITOR_USAGE_SOURCES.fk(), ondelete="SET NULL"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    visitor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    referrer: Mapped[str | None] = mapped_column(Text, nullable=True)
    release: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Trace identity (frontend->backend correlation) + dimensions. See 0038.
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorErrorGroup(Base):
    """A distinct error "issue" — many raw occurrences folded together by a
    deterministic fingerprint, so the dashboard shows a handful of problems
    instead of thousands of rows."""

    __tablename__ = MONITOR_ERROR_GROUPS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey(MONITOR_USAGE_SOURCES.fk(), ondelete="SET NULL"), nullable=True, index=True)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="error")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unresolved")
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_release: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorErrorEvent(Base):
    """A single raw error occurrence, linked to its group and (when available)
    to the visitor/session it happened in — the tie-back to usage data."""

    __tablename__ = MONITOR_ERROR_EVENTS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey(MONITOR_USAGE_SOURCES.fk(), ondelete="SET NULL"), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey(MONITOR_ERROR_GROUPS.fk(), ondelete="CASCADE"), nullable=True, index=True)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="error")
    handled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    release: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    visitor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorWebVital(Base):
    """A single Core Web Vital sample (LCP/CLS/INP/FCP/TTFB) captured by the
    beacon. The per-URL experience rollups (p75 + rating) aggregate these."""

    __tablename__ = MONITOR_WEB_VITALS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey(MONITOR_USAGE_SOURCES.fk(), ondelete="SET NULL"), nullable=True, index=True)
    metric: Mapped[str] = mapped_column(String(10), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    rating: Mapped[str | None] = mapped_column(String(20), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    navigation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    visitor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    release: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorLog(Base):
    """A client log line (debug/info/warn/error) from the beacon's log() call or
    console hook. Faceted by level/session/release in the logs explorer."""

    __tablename__ = MONITOR_LOGS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey(MONITOR_USAGE_SOURCES.fk(), ondelete="SET NULL"), nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    visitor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    release: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorSpan(Base):
    """One unit of work in a trace — a page load, a fetch, a backend request, a
    slow query, or a manually tagged feature. Joined into a trace by trace_id and
    into a call tree by parent_span_id. Usage/error/log/vital rows reference the
    same trace_id, so a frontend error can be tied to the backend span that
    caused it (the incident chain) and journeys can be derived from span order."""

    __tablename__ = MONITOR_SPANS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey(MONITOR_USAGE_SOURCES.fk(), ondelete="SET NULL"), nullable=True, index=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    span_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="client")
    service: Mapped[str | None] = mapped_column(Text, nullable=True)
    feature: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    release: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    visitor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorProblem(Base):
    """A monitoring condition the alert evaluator recorded (error spike, signup
    drop, ...). Separate from the discovery `problems` table. detected_at +
    metric/baseline/observed drive the before/during/after impact view."""

    __tablename__ = MONITOR_PROBLEMS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="warning")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    metric: Mapped[str | None] = mapped_column(Text, nullable=True)
    baseline: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed: Mapped[float | None] = mapped_column(Float, nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorInvestigation(Base):
    """A case object: collects evidence + notes (entries) into a timeline."""

    __tablename__ = MONITOR_INVESTIGATIONS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorInvestigationEntry(Base):
    """One timeline entry: a free note, or a link to an issue/problem/session."""

    __tablename__ = MONITOR_INVESTIGATION_ENTRIES.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    investigation_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey(MONITOR_INVESTIGATIONS.fk(), ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    ref_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorReport(Base):
    """A saved, exportable write-up, optionally tied to an investigation."""

    __tablename__ = MONITOR_REPORTS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    investigation_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey(MONITOR_INVESTIGATIONS.fk(), ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorRevenueSource(Base):
    __tablename__ = "portfolio_revenue_sources"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="stripe")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="not_connected")
    # Credential path. "connected" reads a Connect account via stripe_account;
    # "first_party" reads Immensity's own platform account via STRIPE_SECRET_KEY
    # with no stripe_account param. The engine math is identical either way.
    account_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="connected")
    provider_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_account_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_mrr_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # MRR from the prior sync, kept so a drop can be detected for alerts.
    previous_mrr_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_customers_30d: Mapped[int | None] = mapped_column(Integer, nullable=True)
    churned_customers_30d: Mapped[int | None] = mapped_column(Integer, nullable=True)
    churn_rate_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Parallel invoice-engine totals (PR1). Computed alongside the old snapshot
    # above so the two can be compared before the revenue_engine flag is flipped;
    # the serializer reads whichever the flag selects.
    invoice_mrr_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_invoice_mrr_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invoice_revenue_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Unit-economics inputs (PR2). Null → fall back to config defaults and badge
    # the dependent metric "estimated".
    gross_margin_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    cac_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profit_margin_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    oauth_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_state_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorBillingEvent(Base):
    """Immutable, append-only ledger row derived from a Stripe invoice line item.
    One row per recurring-revenue-affecting fact. ``stripe_line_item_id`` is the
    natural idempotency key, so re-running the backfill never duplicates a row.
    Non-subscription line items (one-off charges, setup fees) are excluded at
    ingestion and never reach this table."""

    __tablename__ = "portfolio_billing_events"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    revenue_source_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("portfolio_revenue_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_invoice_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_line_item_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # This line's normalized monthly amount, after discounts, in cents.
    mrr_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="usd")
    # Line item period.start — when this recurring level takes effect.
    effective_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    is_proration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorCustomerMrr(Base):
    """Per-customer normalized MRR time series. Summing each customer's latest
    row on/before a date gives total MRR at that date."""

    __tablename__ = "portfolio_customer_mrr"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    revenue_source_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("portfolio_revenue_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    stripe_customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    mrr_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="usd")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorMrrMovement(Base):
    """A single MRR movement for a customer, derived by diffing consecutive
    ``portfolio_customer_mrr`` rows. At most one movement per customer per
    ``effective_date``."""

    __tablename__ = "portfolio_mrr_movements"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    revenue_source_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("portfolio_revenue_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    stripe_customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    movement_type: Mapped[str] = mapped_column(Text, nullable=False)
    mrr_delta_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mrr_after_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="usd")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorRevenueDaily(Base):
    """Daily gross revenue for a source, aggregated from Stripe succeeded
    charges. One row per (revenue source, day, currency); ``gross_cents`` is the
    day's gross charge total in that currency's minor units (refunds not netted).

    This is *daily financial activity* — how much money actually came in each
    day — as opposed to the recurring-revenue snapshot in
    ``portfolio_customer_mrr``. It feeds the Portfolio overview revenue card so
    that card reads like traffic/usage/errors: a daily series with a fixed
    prior-7-days comparison."""

    __tablename__ = "portfolio_revenue_daily"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    revenue_source_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("portfolio_revenue_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    gross_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="usd")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorCustomer(Base):
    """A Stripe customer seen on a revenue source's account, keyed by
    ``stripe_customer_id``. Populated from the Stripe customer object during sync
    so the identity email-fallback has an email to match against."""

    __tablename__ = "portfolio_customers"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    revenue_source_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("portfolio_revenue_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    stripe_customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorIdentity(Base):
    """One identified usage actor per product. ``resolution_method`` records how
    ``stripe_customer_id`` was determined: 'explicit' (id trait), 'email'
    (matched a Stripe customer email), or 'unresolved'."""

    __tablename__ = "portfolio_identities"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    user_ref: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    traits: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    resolution_method: Mapped[str] = mapped_column(Text, nullable=False, default="unresolved")
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorAlert(Base):
    """A record that an alert was sent, used to dedupe so the same condition
    doesn't email repeatedly. ``dedupe_key`` is per alert type (e.g. the error
    group id for new-issue, or a date bucket for spike/drop alerts)."""

    __tablename__ = MONITOR_ALERTS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    alert_type: Mapped[str] = mapped_column(String(40), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    recipient: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    sent_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class MonitorAlertSettings(Base):
    """Per-product alert preferences. One row per pipeline; absence means
    "all triggers on, global thresholds". Threshold columns are nullable —
    null falls back to the global default in config."""

    __tablename__ = MONITOR_ALERT_SETTINGS.physical

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    new_issue_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_spike_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    signups_drop_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revenue_drop_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_spike_multiplier: Mapped[float | None] = mapped_column(Float, nullable=True)
    signups_drop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_drop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


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


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    problem_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    pipeline_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="todo")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Optional calendar due date (Phase 2), anchored in the UI to the project's
    # launch window. Day-granular.
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class AppSetting(Base):
    """One admin-editable global config override. ``key`` matches a Settings
    attribute; ``value`` is the override applied over that config default. Only
    whitelisted keys (see app/services/app_settings.py) are ever read/written."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class UserPreference(Base):
    """Per-user preferences: notification delivery + workspace defaults. One row
    per uid; absence means all defaults (see app/services/preferences.py)."""

    __tablename__ = "user_preferences"

    uid: Mapped[str] = mapped_column(Text, primary_key=True)
    alerts_email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    digest_cadence: Mapped[str] = mapped_column(Text, nullable=False, default="instant")  # instant | daily | weekly
    alert_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_pipeline_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_landing: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_digest_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class UserActivityDaily(Base):
    """Per-user daily activity rollup used by the workspace heatmap."""

    __tablename__ = "user_activity_daily"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    activity_date: Mapped[date] = mapped_column(Date, primary_key=True)
    login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    action_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_active_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class GoalDefinition(Base):
    """Static, admin-defined goal (seeded in migration 0042; no user config).
    ``scope`` is 'project' or 'account'; ``metric_key`` maps to a rollup the Goal
    Progress Engine reads; ``icon`` is a lucide icon name (no badge collectibles)."""

    __tablename__ = "goal_definitions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    metric_key: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # False for pre-launch "journey" goals (shown while a project is still on the
    # way to launch); True (default) for post-launch outcome goals.
    requires_launch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class GoalTier(Base):
    """One ordered threshold for a goal. ``threshold_value`` is compared to the
    goal's current metric value; ``label`` is the human tier name."""

    __tablename__ = "goal_tiers"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    goal_definition_id: Mapped[str] = mapped_column(Text, ForeignKey("goal_definitions.id", ondelete="CASCADE"), nullable=False, index=True)
    tier_index: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    # Configured duration (days) a tier is expected to take once it becomes the
    # active milestone. NULL falls back to a computed, scale-aware estimate.
    # The clock starts only on activation — see services/goals.py.
    estimate_days: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ProjectGoalAchievement(Base):
    """Append-only record of a project crossing a goal tier (the milestone log)."""

    __tablename__ = "project_goal_achievements"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False, index=True)
    goal_definition_id: Mapped[str] = mapped_column(Text, ForeignKey("goal_definitions.id", ondelete="CASCADE"), nullable=False)
    tier_index: Mapped[int] = mapped_column(Integer, nullable=False)
    achieved_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class AccountGoalAchievement(Base):
    """Append-only record of an account (user) crossing a portfolio-level goal
    tier. Same shape as the project table with no project_id."""

    __tablename__ = "account_goal_achievements"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    goal_definition_id: Mapped[str] = mapped_column(Text, ForeignKey("goal_definitions.id", ondelete="CASCADE"), nullable=False)
    tier_index: Mapped[int] = mapped_column(Integer, nullable=False)
    achieved_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
