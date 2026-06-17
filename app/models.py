import enum
import uuid
from app.db import Base
from datetime import datetime, date, timezone
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID, ARRAY
from sqlalchemy import String, Text, Integer, Boolean, Float, DateTime, Date, TIMESTAMP, ForeignKey
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

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
# six tables are: pipeline_runs, clusters, cluster_items, cluster_snapshots,
# cluster_neighbors, cluster_signals. Only the three the MVP reads are modelled
# here (clusters, cluster_items, cluster_snapshots).
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
    name: Mapped[str] = mapped_column(Text, nullable=False)
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
    launched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


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
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    team_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True)
    pipeline_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey("pipeline.id", ondelete="SET NULL"), nullable=True, index=True)
    parent_issue_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), ForeignKey("issues.id", ondelete="CASCADE"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
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
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
