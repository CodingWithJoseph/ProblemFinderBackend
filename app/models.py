import enum
import uuid
from app.db import Base
from datetime import datetime, date, timezone
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR, JSONB, UUID as PGUUID, ARRAY
from sqlalchemy import String, Text, Integer, Boolean, Float, Numeric, DateTime, Date, TIMESTAMP
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
    domain: Mapped[str | None] = mapped_column(String(100), nullable=True)
    type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    upvotes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trending: Mapped[bool] = mapped_column(Boolean, default=False)
    wtp_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    workaround_boolean: Mapped[bool] = mapped_column(Boolean, default=False)
    intensity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    signal_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    intensity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    wtp_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    workaround_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # --- Section 2 additions (must stay in sync with 0003_alter_existing.sql:
    #     hybrid_search RETURNS SETOF opportunities, so /search rebuilds
    #     Opportunity(**row) from SELECT * — every column must be mapped here) ---
    top_comments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    signal_strength: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-post intent (venting / seeking / workaround / purchase_intent /
    # cost_complaint), set by the AI scoring pipeline. See migration 0004.
    post_intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    pain_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    urgency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    commercial_intent_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    solution_seeking_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    primary_problem: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_audience: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_persona: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    monetization_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_willingness_to_pay: Mapped[float | None] = mapped_column(Float, nullable=True)
    competition_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_saturation: Mapped[str | None] = mapped_column(Text, nullable=True)
    revenue_potential_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommended_next_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_review: Mapped[bool | None] = mapped_column(Boolean, default=False)
    model_version: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


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
    signal_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    trending: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    growth_rate_wow: Mapped[float | None] = mapped_column(Float, nullable=True)
    growth_rate_mom: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_velocity: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    audience_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    problem_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    monetization_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    community_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_pain_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_urgency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_commercial_intent_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_solution_seeking_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    growth_acceleration: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_potential_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    competition_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    saturation_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClusterSnapshot(Base):
    __tablename__ = "cluster_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_posts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unique_author_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unique_community_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_comments: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_pain_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_urgency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_commercial_intent_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_solution_seeking_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    growth_acceleration: Mapped[float | None] = mapped_column(Float, nullable=True)
    engagement_velocity: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_potential_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_keywords: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    top_entities: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    signal_score: Mapped[float | None] = mapped_column(Float, nullable=True)


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
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_post_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list | None] = mapped_column(Vector(1536), nullable=True)
    pain_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    urgency_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    frequency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    solution_seeking_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_alternatives: Mapped[str | None] = mapped_column(Text, nullable=True)
    willingness_to_pay_signal: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    problem_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    pipeline_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), nullable=False)
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
    source_post_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    membership_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    assigned_by: Mapped[str | None] = mapped_column(Text, default="model")
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))


class ClusterScore(Base):
    __tablename__ = "cluster_scores"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[int] = mapped_column(Integer, nullable=False)
    score_type: Mapped[str] = mapped_column(Text, nullable=False)
    score_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))


class RevenueSignal(Base):
    __tablename__ = "revenue_signals"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opportunity_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    pipeline_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    offer_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_point: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    revenue_amount: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    revenue_period: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversion_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reported_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opportunity_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    pipeline_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    experiment_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    hypothesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_audience: Mapped[str | None] = mapped_column(Text, nullable=True)
    offer: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="planned")
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))


class ExperimentResult(Base):
    __tablename__ = "experiment_results"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    experiment_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signups: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replies: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversion_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_generated: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    cost: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))


class ModelRun(Base):
    __tablename__ = "model_runs"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_table: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_table: Mapped[str | None] = mapped_column(Text, nullable=True)
    parameters: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="running")


class TrainingExample(Base):
    __tablename__ = "training_examples"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    mrr: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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
    avg_revenue_potential_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_commercial_intent_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_pain_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_reported_revenue: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    avg_conversion_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_clusters: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
