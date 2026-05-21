from datetime import datetime
from sqlalchemy import (
    String, Text, Integer, Float, Boolean,
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
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0)
    upvotes: Mapped[int] = mapped_column(Integer, default=0)
    trending: Mapped[bool] = mapped_column(Boolean, default=False)
    wtp_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    workaround: Mapped[str | None] = mapped_column(Text, nullable=True)
    intensity: Mapped[IntensityEnum] = mapped_column(SAEnum(IntensityEnum), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)

    pipeline_cards: Mapped[list["PipelineCard"]] = relationship(back_populates="opportunity")
    portfolio_products: Mapped[list["PortfolioProduct"]] = relationship(back_populates="opportunity")


class PipelineCard(Base):
    __tablename__ = "pipeline_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    stage: Mapped[StageEnum] = mapped_column(SAEnum(StageEnum), default=StageEnum.watching)
    kill_criteria: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    distribution_channels: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    concept_angles: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    exit_checklist: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    opportunity: Mapped["Opportunity"] = relationship(back_populates="pipeline_cards")


class PortfolioProduct(Base):
    __tablename__ = "portfolio_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revenue_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[ProductStatusEnum] = mapped_column(SAEnum(ProductStatusEnum), default=ProductStatusEnum.active)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    opportunity: Mapped["Opportunity | None"] = relationship(back_populates="portfolio_products")


class SearchUsage(Base):
    __tablename__ = "search_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    query: Mapped[str] = mapped_column(String(500), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    searched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"

    uid: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan: Mapped[PlanEnum] = mapped_column(SAEnum(PlanEnum), default=PlanEnum.free)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)