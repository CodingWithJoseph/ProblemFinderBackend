"""Strict contracts for summaries grounded in server-loaded Search evidence."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


SEARCH_SUMMARY_MAX_CLUSTERS = 5
SEARCH_SUMMARY_MAX_EVIDENCE = 25
CitationId = str


class SearchSummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    search_run_id: UUID
    cluster_ids: list[int] = Field(min_length=1, max_length=SEARCH_SUMMARY_MAX_CLUSTERS)
    confirmed: Literal[True]
    web_query: str | None = Field(default=None, min_length=2, max_length=500)

    @field_validator("cluster_ids")
    @classmethod
    def unique_positive_cluster_ids(cls, values: list[int]) -> list[int]:
        if any(value < 1 for value in values):
            raise ValueError("cluster IDs must be positive")
        if len(set(values)) != len(values):
            raise ValueError("cluster IDs must be unique")
        return values


class SearchSummaryFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=500)
    citation_ids: list[CitationId] = Field(min_length=1, max_length=5)

    @field_validator("citation_ids")
    @classmethod
    def unique_citations(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 100 for value in values):
            raise ValueError("invalid citation ID")
        if len(set(values)) != len(values):
            raise ValueError("citation IDs must be unique")
        return values


class SearchSummaryModelOutput(BaseModel):
    """Schema-constrained model output before dynamic citation validation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    overview: str = Field(min_length=1, max_length=1_200)
    overview_citation_ids: list[CitationId] = Field(min_length=1, max_length=5)
    findings: list[SearchSummaryFinding] = Field(min_length=1, max_length=5)
    limitations: list[str] = Field(max_length=5)

    @field_validator("overview_citation_ids")
    @classmethod
    def unique_overview_citations(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 100 for value in values):
            raise ValueError("invalid overview citation ID")
        if len(set(values)) != len(values):
            raise ValueError("overview citation IDs must be unique")
        return values

    @field_validator("limitations")
    @classmethod
    def normalize_limitations(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = raw_value.strip()
            if not value:
                continue
            if len(value) > 300:
                raise ValueError("limitations must be 300 characters or fewer")
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                normalized.append(value)
        return normalized


class SearchSummaryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_id: CitationId = Field(min_length=1, max_length=100)
    source_type: Literal["database_cluster", "database_item", "web"]
    label: str = Field(min_length=1, max_length=300)
    cluster_id: int | None = Field(default=None, ge=1)
    url: str | None = Field(default=None, max_length=2_000)


class SearchSummaryResponse(SearchSummaryModelOutput):
    model_config = ConfigDict(extra="forbid")

    evidence: list[SearchSummaryEvidence] = Field(max_length=SEARCH_SUMMARY_MAX_EVIDENCE)
    provider: Literal["local", "groq"]
