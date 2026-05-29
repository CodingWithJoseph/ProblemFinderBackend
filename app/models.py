import enum
import uuid
from app.db import Base
from datetime import datetime, date, timezone
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


class ClusterSnapshot(Base):
    __tablename__ = "cluster_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_posts: Mapped[list | None] = mapped_column(JSONB, nullable=True)


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
