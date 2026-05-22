from datetime import datetime, timezone
from sqlalchemy import (
    String, Text, Integer, Boolean,
    DateTime, ForeignKey, Enum as SAEnum
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base
import enum


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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    domain: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    upvotes: Mapped[int] = mapped_column(Integer, default=0)
    trending: Mapped[bool] = mapped_column(Boolean, default=False)
    wtp_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    workaround: Mapped[str | None] = mapped_column(Text, nullable=True)
    intensity: Mapped[IntensityEnum] = mapped_column(SAEnum(IntensityEnum), nullable=False)
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
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)