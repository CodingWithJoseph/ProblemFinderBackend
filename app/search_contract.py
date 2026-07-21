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
