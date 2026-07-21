"""Validated contract shared by deterministic and conversational search.

The conversational interpreter will produce this shape in a later change.  The
database query layer only accepts these allow-listed fields; it never accepts
SQL, column names, or arbitrary operators from a client or model.
"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 50
SEARCH_MAX_FILTER_VALUES = 20

SearchSort = Literal["relevance", "newest", "largest", "trending", "signal_score"]


class ClusterSearchQuery(BaseModel):
    """Allow-listed, deterministic cluster search inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str | None = Field(default=None, max_length=500)
    opportunity_domains: list[str] = Field(default_factory=list, max_length=SEARCH_MAX_FILTER_VALUES)
    opportunity_types: list[str] = Field(default_factory=list, max_length=SEARCH_MAX_FILTER_VALUES)
    sources: list[str] = Field(default_factory=list, max_length=SEARCH_MAX_FILTER_VALUES)
    communities: list[str] = Field(default_factory=list, max_length=SEARCH_MAX_FILTER_VALUES)
    min_posts: int = Field(default=1, ge=1, le=100_000)
    observed_after: datetime | None = None
    trending_only: bool = False
    min_signal_score: float | None = Field(default=None, ge=0, le=1)
    sort: SearchSort = "relevance"
    limit: int = Field(default=SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT)
    offset: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if len(value) < 2:
            raise ValueError("query must contain at least 2 characters")
        return value

    @field_validator(
        "opportunity_domains",
        "opportunity_types",
        "sources",
        "communities",
    )
    @classmethod
    def normalize_filter_values(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = raw_value.strip()
            if not value or value.casefold() == "all":
                continue
            if len(value) > 100:
                raise ValueError("filter values must be 100 characters or fewer")
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                normalized.append(value)
        return normalized

    @field_validator("observed_after")
    @classmethod
    def normalize_observed_after(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def applied_filters(self) -> dict:
        """Stable response shape the frontend can render as confirmation chips."""
        return {
            "query": self.query,
            "opportunity_domains": self.opportunity_domains,
            "opportunity_types": self.opportunity_types,
            "sources": self.sources,
            "communities": self.communities,
            "min_posts": self.min_posts,
            "observed_after": self.observed_after.isoformat() if self.observed_after else None,
            "trending_only": self.trending_only,
            "min_signal_score": self.min_signal_score,
            "sort": self.sort,
        }


class SearchInterpretRequest(BaseModel):
    """One conversational turn plus the draft produced by prior turns."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=2, max_length=2_000)
    current_draft: ClusterSearchQuery | None = None


class SearchFilterOptions(BaseModel):
    """Canonical values currently present in the searchable dataset."""

    model_config = ConfigDict(extra="forbid")

    opportunity_domains: list[str] = Field(default_factory=list, max_length=100)
    opportunity_types: list[str] = Field(default_factory=list, max_length=100)
    sources: list[str] = Field(default_factory=list, max_length=100)
    communities: list[str] = Field(default_factory=list, max_length=100)

    @field_validator(
        "opportunity_domains",
        "opportunity_types",
        "sources",
        "communities",
    )
    @classmethod
    def normalize_options(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = raw_value.strip()
            if not value or len(value) > 100:
                continue
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                normalized.append(value)
        return normalized


class SearchInterpretationModelOutput(BaseModel):
    """Strict structured output returned by the model before server checks."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft: ClusterSearchQuery
    assumptions: list[str] = Field(max_length=10)
    unsupported: list[str] = Field(max_length=10)
    clarification_question: str | None = Field(max_length=300)

    @field_validator("assumptions", "unsupported")
    @classmethod
    def normalize_notes(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = raw_value.strip()
            if not value:
                continue
            if len(value) > 300:
                raise ValueError("notes must be 300 characters or fewer")
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                normalized.append(value)
        return normalized

    @field_validator("clarification_question")
    @classmethod
    def normalize_question(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()


class SearchInterpretationResponse(BaseModel):
    """Frontend-ready confirmation state; this endpoint never runs the query."""

    draft: ClusterSearchQuery
    confirmation: str
    assumptions: list[str]
    unsupported: list[str]
    clarification_question: str | None
    needs_clarification: bool
    needs_confirmation: bool
    fallback_used: bool
    available_options: SearchFilterOptions
