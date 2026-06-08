import enum
import uuid
from app.db import Base
from datetime import datetime, date, timezone
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR, JSONB, UUID as PGUUID, ARRAY
from sqlalchemy import String, Text, Integer, Boolean, Float, DateTime, Date, TIMESTAMP
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column


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


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    opportunity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    trending: Mapped[bool] = mapped_column(Boolean, default=False)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    num_comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upvote_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    feed: Mapped[str | None] = mapped_column(String(50), nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now(timezone.utc))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pipeline_count: Mapped[int] = mapped_column(Integer, default=0)
    launch_count: Mapped[int] = mapped_column(Integer, default=0)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    # NOTE: hybrid_search RETURNS SETOF opportunities and the /search route
    # rebuilds Opportunity(**row) from SELECT *. The reconstruction filters the
    # row down to mapped columns, so live ``opportunities`` columns we don't map
    # (embedding, opportunity_domain, …) are ignored rather than passed in.
    top_comments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    recommended_next_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_version: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class UserFlag(Base):
    __tablename__ = "user_flags"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
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


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    trending: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    growth_rate_wow: Mapped[float | None] = mapped_column(Float, nullable=True)
    growth_rate_mom: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_velocity: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Rolled up from child opportunities by the AI pipeline: the most-frequent
    # child value as the primary (TEXT) plus the distinct set (JSONB). Filtered
    # on the scalar columns in /clusters/browse + /clusters/search.
    opportunity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    opportunity_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    opportunity_types: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    opportunity_domains: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # --- Pipeline-produced cluster analytics (see migration 0011) ---------
    # Pre-generated by the AI pipeline and read (not written) by the API. The
    # discovery brief prompt is built from summary + top_tfidf_terms +
    # source_breakdown; the rest power the Signal-page analytics.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    top_tfidf_terms: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_breakdown: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    post_volume_by_date: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    opportunity_type_counts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    opportunity_domain_counts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    intra_cluster_density: Mapped[float | None] = mapped_column(Float, nullable=True)
    silhouette_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    persistence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    author_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    community_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    growth_acceleration: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Product-facing Signal Overview generated by ProblemFinderAI. The API only
    # reads and passes through this payload and its generation metadata.
    signal_overview: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    overview_schema_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    overview_generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    overview_model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    overview_input_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    overview_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    overview_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Single-post clusters (see migration add_cluster_is_singleton.sql). Public
    # endpoints exclude these by default.
    is_singleton: Mapped[bool] = mapped_column(Boolean, default=False)
    # --- Staged opportunity briefs (see migration 0011) -------------------
    # Each stage owns a JSONB column, an API route and a server-side prompt. The
    # generated_at timestamp lives *inside* each brief document; the API treats
    # a brief as stale after 7 days.
    #   discovery  → Signal page           (implemented)
    #   market     → Research / Market page (coming soon)
    #   validation → Validate page          (coming soon)
    #   build      → Building / Report page  (the migrated legacy founder brief)
    brief_discovery: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    brief_market: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    brief_validation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    brief_build: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # build-stage metadata (legacy founder-brief refresh stamps these)
    brief_generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    brief_model_version: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClusterSnapshot(Base):
    __tablename__ = "cluster_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    post_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_posts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # avg_comments is read by the AI pipeline's trending detector; the other
    # per-snapshot aggregates were dropped (unused by API + UI). See 0008.
    avg_comments: Mapped[float | None] = mapped_column(Float, nullable=True)


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


# ============================================================
# New tables (0002_new_tables.sql)
# ============================================================


class SourcePost(Base):
    __tablename__ = "source_posts"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    pipeline_version: Mapped[str | None] = mapped_column(Text, default="v1")
    source_post_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_community_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_author_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    permalink: Mapped[str | None] = mapped_column(Text, nullable=True)
    top_comments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    scraped_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upvote_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    num_comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_deleted: Mapped[bool | None] = mapped_column(Boolean, default=False)
    is_removed: Mapped[bool | None] = mapped_column(Boolean, default=False)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))


class ClusterMember(Base):
    __tablename__ = "cluster_members"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[int] = mapped_column(Integer, nullable=False)
    pipeline_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_post_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    membership_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    assigned_by: Mapped[str | None] = mapped_column(Text, default="model")
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))


class DomainSnapshot(Base):
    __tablename__ = "domain_snapshots"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    cluster_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opportunity_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_signal_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_growth_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_clusters: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
